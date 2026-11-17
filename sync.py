#!/usr/bin/env python3
"""sync: share progress (cards, problems and bugs) between this CLI and the web app.

  sync.py              push new local attempts, pull new web attempts, rebuild schedules
  sync.py --status     show the configuration and what is pending
Env: DRILL_URL (https://<worker host>), DRILL_CLIENT_ID and DRILL_CLIENT_SECRET (a Cloudflare
Access service token that the application's policy allows; not needed for http://localhost).
`problems.py solve` and `bugs.py next` call this automatically when DRILL_URL is set.

Attempts are append-only events with a unique id. Each side rebuilds a card's schedule by
replaying its events in time order, so devices converge whatever order they sync in.
Answer texts stay where they were typed; only the scores and timing travel.
"""
import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Callable

import drill
import problems

BATCH = 200  # attempts per push request (the Worker accepts at most 200)
PULL = 500  # attempts per pull request
Transport = Callable[[str, str, "dict | None"], dict]


class SyncError(RuntimeError):
    pass


def to_z(ts: str) -> str:
    """Local timestamps end in +00:00; the Worker uses Z."""
    return ts[:-6] + "Z" if ts.endswith("+00:00") else ts


def from_z(ts: str) -> str:
    return ts[:-1] + "+00:00" if ts.endswith("Z") else ts


def quality(row) -> float:
    """Quality (0..5) of an attempt; rows from before sync have no stored q."""
    if row["q"] is not None:
        return float(row["q"])
    passed, total = row["passed"], row["total"]
    if row["key"].startswith("problem:"):
        return 5.0 if passed == total else 3.0 if passed >= 0.8 * total else 1.0
    return float(passed)  # bug attempts stored round(q) out of 5


def get_state(db, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(db, key: str, value: str) -> None:
    db.execute("INSERT INTO sync_state VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
               "value = excluded.value", (key, value))


def attempt_json(row) -> dict:
    return {"uid": row["uid"], "key": row["key"], "ts": to_z(row["ts"]), "q": quality(row),
            "passed": row["passed"], "total": row["total"], "secs": 0}


def card_attempt_json(row) -> dict:
    """A regular card attempt on the wire; its uid is namespaced so it cannot meet an exercise's."""
    return {"uid": row["uid"], "key": row["card_id"], "ts": to_z(row["ts"]),
            "q": float(row["score"] or 0), "secs": round(row["secs"] or 0),
            "correct": row["correct"], "depth": row["depth"], "clarity": row["clarity"]}


def is_exercise(key: str) -> bool:
    return key.startswith(("problem:", "bug:"))


def assign_uids(db) -> None:
    """Give attempts made on this device (before sync existed, or since the last sync) a uid."""
    db.execute("UPDATE attempts SET uid = ? || ':c' || id WHERE uid IS NULL",
               (problems.device_id(db),))
    db.commit()


def replay_card_local(db, card_id: str) -> None:
    """Rebuild the schedule of a regular card from its attempts, oldest first."""
    rows = db.execute("SELECT ts, COALESCE(score, 0) AS q FROM attempts WHERE card_id = ? "
                      "ORDER BY ts, id", (card_id,)).fetchall()
    if not rows:
        return
    ease, interval, reps = 2.5, 0.0, 0
    for r in rows:
        ease, interval, reps = drill.schedule(ease, interval, reps, r["q"])
    due = (datetime.fromisoformat(rows[-1]["ts"]) + timedelta(days=interval))
    db.execute("UPDATE cards SET ease = ?, interval = ?, reps = ?, due = ? WHERE id = ?",
               (ease, interval, reps, due.isoformat(timespec="seconds"), card_id))


def replay_local(db, key: str) -> None:
    """Rebuild ex_state for `key` from its attempts, oldest first (same fold as the Worker)."""
    rows = db.execute("SELECT key, ts, passed, total, q FROM ex_attempts WHERE key = ? "
                      "ORDER BY ts, id", (key,)).fetchall()
    if not rows:
        return
    ease, interval, reps = 2.5, 0.0, 0
    for r in rows:
        ease, interval, reps = drill.schedule(ease, interval, reps, quality(r))
    last = datetime.fromisoformat(rows[-1]["ts"])
    due = (last + timedelta(days=interval)).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO ex_state(key, ease, interval, reps, due) VALUES(?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET ease = excluded.ease, interval = excluded.interval, "
        "reps = excluded.reps, due = excluded.due",
        (key, ease, interval, reps, due),
    )


def push_table(db, transport: Transport, table: str, to_json: Callable, out: dict) -> None:
    """Send this device's unconfirmed rows of `table` in batches, oldest first."""
    dev = problems.device_id(db)
    after = 0
    while True:
        rows = db.execute(f"SELECT * FROM {table} WHERE pushed = 0 AND uid LIKE ? AND id > ? "
                          "ORDER BY id LIMIT ?", (f"{dev}:%", after, BATCH)).fetchall()
        if not rows:
            return
        after = rows[-1]["id"]
        wire = [to_json(r) for r in rows]
        resp = transport("POST", "/api/sync/push", {"attempts": wire})
        # keys not published to the web yet stay unpushed and are retried next time
        skipped = set(resp.get("skipped", []))
        db.executemany(f"UPDATE {table} SET pushed = 1 WHERE id = ?",
                       [(r["id"],) for r, w in zip(rows, wire) if w["key"] not in skipped])
        db.commit()
        out["pushed"] += int(resp.get("accepted", 0))
        out["duplicates"] += int(resp.get("duplicates", 0))
        out["skipped"] |= skipped


def push(db, transport: Transport) -> dict:
    """Send attempts made on this device that the Worker has not confirmed yet."""
    assign_uids(db)
    out = {"pushed": 0, "duplicates": 0, "skipped": set()}
    push_table(db, transport, "ex_attempts", attempt_json, out)
    push_table(db, transport, "attempts", card_attempt_json, out)
    return out


def pull(db, transport: Transport) -> tuple[int, set[str]]:
    """Fetch attempts the Worker has that this database lacks. Returns (new count, keys)."""
    cursor = int(get_state(db, "pull_cursor", "0"))
    new, touched = 0, set()
    while True:
        resp = transport("GET", f"/api/sync/pull?after={cursor}&limit={PULL}", None)
        rows = resp.get("attempts", [])
        for a in rows:
            if is_exercise(a["key"]):
                passed, total = a.get("passed"), a.get("total")
                if passed is None or total is None:  # a graded answer without test counts
                    passed, total = round(a["q"]), 5
                cur = db.execute(
                    "INSERT OR IGNORE INTO ex_attempts"
                    "(key, ts, passed, total, detail, q, uid, pushed) VALUES(?,?,?,?,?,?,?,1)",
                    (a["key"], from_z(a["ts"]), passed, total, json.dumps({"mode": "web"}),
                     a["q"], a["uid"]))
            else:
                level = round(a["q"])  # older servers send no per-criterion grades
                grades = [a.get(k) if a.get(k) is not None else level
                          for k in ("correct", "depth", "clarity")]
                cur = db.execute(
                    "INSERT OR IGNORE INTO attempts(card_id, ts, secs, answer, correct, depth, "
                    "clarity, score, detail, uid, pushed) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                    (a["key"], from_z(a["ts"]), a.get("secs") or 0, "(web)", *grades, a["q"],
                     json.dumps({"mode": "web"}), a["uid"]))
            if cur.rowcount > 0:
                new += 1
                touched.add(a["key"])
        if rows:
            if int(resp["next"]) <= cursor:
                raise SyncError("the server's cursor did not advance")
            cursor = int(resp["next"])
            set_state(db, "pull_cursor", str(cursor))
            db.commit()
        if len(rows) < PULL:
            return new, touched


def sync(db, transport: Transport) -> dict:
    pushed = push(db, transport)
    new, touched = pull(db, transport)
    for key in sorted(touched):
        (replay_local if is_exercise(key) else replay_card_local)(db, key)
    db.commit()
    return {"pushed": pushed["pushed"], "duplicates": pushed["duplicates"],
            "skipped": sorted(pushed["skipped"]), "pulled": new}


class Http:
    """JSON over HTTPS with an Access service token (headers only added when configured)."""

    def __init__(self, url: str, client_id: str = "", secret: str = "", timeout: float = 30):
        self.url = url.rstrip("/")
        self.client_id, self.secret, self.timeout = client_id, secret, timeout

    def __call__(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json", "X-Drill": "1",
                   "User-Agent": "drill-sync/1"}
        if self.client_id:
            headers["CF-Access-Client-Id"] = self.client_id
            headers["CF-Access-Client-Secret"] = self.secret
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise SyncError(f"HTTP {exc.code}: {detail}") from exc
        except OSError as exc:  # URLError, timeouts, refused connections
            raise SyncError(f"cannot reach {self.url}: {exc}") from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise SyncError("unexpected response (not JSON): check DRILL_URL, DRILL_CLIENT_ID, "
                            "DRILL_CLIENT_SECRET and the Access policy") from exc


def transport_from_env() -> Http:
    url = os.environ.get("DRILL_URL", "")
    if not url:
        raise SyncError("DRILL_URL is not set")
    return Http(url, os.environ.get("DRILL_CLIENT_ID", ""),
                os.environ.get("DRILL_CLIENT_SECRET", ""))


def cmd_status(db) -> None:
    mine = f"{problems.device_id(db)}:%"
    unpushed = sum(
        db.execute(f"SELECT COUNT(*) FROM {t} WHERE pushed = 0 AND (uid LIKE ? OR uid IS NULL)",
                   (mine,)).fetchone()[0] for t in ("ex_attempts", "attempts"))
    creds = bool(os.environ.get("DRILL_CLIENT_ID") and os.environ.get("DRILL_CLIENT_SECRET"))
    print(f"url: {os.environ.get('DRILL_URL') or '(DRILL_URL not set)'}\n"
          f"service token: {'set' if creds else 'not set'}\n"
          f"device: {problems.device_id(db)}\n"
          f"attempts not yet pushed: {unpushed}\n"
          f"pull cursor: {get_state(db, 'pull_cursor', '0')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    db = problems.connect()
    if args.status:
        return cmd_status(db)
    try:
        stats = sync(db, transport_from_env())
    except SyncError as exc:
        raise SystemExit(f"sync failed: {exc}") from exc
    print(f"pushed {stats['pushed']} (already there: {stats['duplicates']}), "
          f"pulled {stats['pulled']}")
    if stats["skipped"]:
        print(f"not on the web yet (run npm run cards): {', '.join(stats['skipped'])}")


if __name__ == "__main__":
    main()
