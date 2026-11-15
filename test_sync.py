import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import drill
import kata
import sync

FIXTURE = Path(__file__).parent / "web" / "test" / "fixtures" / "sync-push.json"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in [("HOME", self.root / "home"), ("CARDS", self.root / "cards")]:
            patcher = mock.patch.object(drill, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.root / "cards").mkdir()

    def insert(self, db, key, ts, q, passed, total, uid, pushed=0):
        db.execute("INSERT INTO ex_attempts(key, ts, passed, total, detail, q, uid, pushed) "
                   "VALUES(?,?,?,?,?,?,?,?)", (key, ts, passed, total, "", q, uid, pushed))
        db.commit()


class Fake:
    """A transport that records calls and answers from a script of callables or dicts."""

    def __init__(self, *responses):
        self.calls, self.responses = [], list(responses)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        r = self.responses.pop(0)
        return r(method, path, body) if callable(r) else r


class MigrationTest(Base):
    def test_legacy_database_is_upgraded_in_place(self):
        (self.root / "home").mkdir()
        raw = sqlite3.connect(self.root / "home" / "drill.db")
        raw.executescript(
            "CREATE TABLE ex_state(key TEXT PRIMARY KEY, ease REAL DEFAULT 2.5, "
            "interval REAL DEFAULT 0, reps INTEGER DEFAULT 0, due TEXT DEFAULT '');"
            "CREATE TABLE ex_attempts(id INTEGER PRIMARY KEY, key TEXT, ts TEXT, passed INTEGER,"
            " total INTEGER, detail TEXT);"
            "INSERT INTO ex_attempts(key, ts, passed, total, detail) VALUES"
            "('kata:a', '2026-11-01T10:00:00+00:00', 3, 3, ''),"
            "('bug:b', '2026-11-02T10:00:00+00:00', 4, 5, '{}');")
        raw.commit()
        raw.close()
        db = kata.connect()
        dev = kata.device_id(db)
        rows = db.execute("SELECT id, uid, q, pushed FROM ex_attempts ORDER BY id").fetchall()
        self.assertEqual([(r["uid"], r["q"], r["pushed"]) for r in rows],
                         [(f"{dev}:1", None, 0), (f"{dev}:2", None, 0)])
        again = kata.connect()  # idempotent, same device
        self.assertEqual(kata.device_id(again), dev)
        self.assertEqual(again.execute("SELECT COUNT(*) FROM ex_attempts").fetchone()[0], 2)

    def test_record_stores_quality_and_a_unique_uid(self):
        db = kata.connect()
        kata.record(db, "kata:a", 5, 3, 3)
        kata.record(db, "kata:a", 3, 2, 3)
        rows = db.execute("SELECT q, uid, pushed FROM ex_attempts ORDER BY id").fetchall()
        dev = kata.device_id(db)
        self.assertEqual([r["q"] for r in rows], [5, 3])
        self.assertTrue(all(r["uid"].startswith(f"{dev}:") and r["pushed"] == 0 for r in rows))
        self.assertEqual(len({r["uid"] for r in rows}), 2)

    def test_quality_of_legacy_rows(self):
        def row(key, passed, total, q=None):
            return {"key": key, "passed": passed, "total": total, "q": q}

        self.assertEqual(sync.quality(row("kata:a", 15, 15)), 5.0)
        self.assertEqual(sync.quality(row("kata:a", 12, 15)), 3.0)
        self.assertEqual(sync.quality(row("kata:a", 3, 15)), 1.0)
        self.assertEqual(sync.quality(row("bug:b", 4, 5)), 4.0)
        self.assertEqual(sync.quality(row("bug:b", 4, 5, q=3.7)), 3.7)

    def test_timestamp_formats(self):
        self.assertEqual(sync.to_z("2026-11-14T10:20:30+00:00"), "2026-11-14T10:20:30Z")
        self.assertEqual(sync.from_z("2026-11-14T10:20:30Z"), "2026-11-14T10:20:30+00:00")
        self.assertEqual(sync.to_z("2026-11-14T10:20:30Z"), "2026-11-14T10:20:30Z")


class ReplayTest(Base):
    def test_replay_equals_live_scheduling(self):
        clock = [0.0]

        def fake_now(delta=0.0):
            base = drill.datetime.fromisoformat("2026-11-01T09:00:00+00:00")
            t = base + drill.timedelta(days=clock[0] + delta)
            return t.isoformat(timespec="seconds")

        db = kata.connect()
        with mock.patch.object(drill, "now", fake_now):
            for day, q in [(0, 1), (1, 5), (2, 5), (5, 3), (6, 0), (7, 5), (8, 5), (12, 4.2)]:
                clock[0] = day
                kata.record(db, "bug:x", q, round(q), 5)
        live = dict(db.execute("SELECT ease, interval, reps, due FROM ex_state "
                               "WHERE key = 'bug:x'").fetchone())
        db.execute("DELETE FROM ex_state")
        sync.replay_local(db, "bug:x")
        replayed = dict(db.execute("SELECT ease, interval, reps, due FROM ex_state "
                                   "WHERE key = 'bug:x'").fetchone())
        self.assertEqual(replayed, live)

    def test_replay_orders_by_time_not_by_insertion(self):
        db = kata.connect()
        self.insert(db, "kata:a", "2026-11-05T09:00:00+00:00", 3, 12, 15, "d:1")
        self.insert(db, "kata:a", "2026-11-01T09:00:00+00:00", 1, 3, 15, "d:2")
        self.insert(db, "kata:a", "2026-11-02T09:00:00+00:00", 5, 15, 15, "d:3")
        sync.replay_local(db, "kata:a")
        r = dict(db.execute("SELECT ease, interval, reps FROM ex_state").fetchone())
        s = (2.5, 0.0, 0)
        for q in (1, 5, 3):
            s = drill.schedule(*s, q)
        self.assertEqual((r["ease"], r["interval"], r["reps"]), s)

    def test_replay_of_nothing_changes_nothing(self):
        db = kata.connect()
        sync.replay_local(db, "kata:none")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM ex_state").fetchone()[0], 0)


class PushTest(Base):
    def seed(self, db):
        dev = kata.device_id(db)
        self.insert(db, "kata:a", "2026-11-01T09:00:00+00:00", 5, 15, 15, f"{dev}:1")
        self.insert(db, "bug:b", "2026-11-02T09:00:00+00:00", 3.5, 4, 5, f"{dev}:2")
        self.insert(db, "kata:a", "2026-11-03T09:00:00+00:00", 5, 15, 15, "web:7", pushed=1)
        self.insert(db, "kata:a", "2026-11-04T09:00:00+00:00", 5, 15, 15, "otherdev:9", pushed=1)
        return dev

    def test_pushes_only_own_unpushed_attempts_and_marks_them(self):
        db = kata.connect()
        dev = self.seed(db)
        t = Fake({"accepted": 2, "duplicates": 0, "skipped": []})
        out = sync.push(db, t)
        (method, path, body), = t.calls
        self.assertEqual((method, path), ("POST", "/api/sync/push"))
        self.assertEqual(body["attempts"], [
            {"uid": f"{dev}:1", "key": "kata:a", "ts": "2026-11-01T09:00:00Z", "q": 5.0,
             "passed": 15, "total": 15, "secs": 0},
            {"uid": f"{dev}:2", "key": "bug:b", "ts": "2026-11-02T09:00:00Z", "q": 3.5,
             "passed": 4, "total": 5, "secs": 0},
        ])
        self.assertEqual((out["pushed"], out["duplicates"], out["skipped"]), (2, 0, set()))
        left = db.execute("SELECT COUNT(*) FROM ex_attempts WHERE pushed = 0").fetchone()[0]
        self.assertEqual(left, 0)
        self.assertEqual(sync.push(db, Fake())["pushed"], 0)  # nothing left, no request made

    def test_skipped_keys_stay_pending_for_the_next_sync(self):
        db = kata.connect()
        self.seed(db)
        out = sync.push(db, Fake({"accepted": 1, "duplicates": 0, "skipped": ["bug:b"]}))
        self.assertEqual(out["skipped"], {"bug:b"})
        pending = db.execute("SELECT key FROM ex_attempts WHERE pushed = 0").fetchall()
        self.assertEqual([r["key"] for r in pending], ["bug:b"])
        t = Fake({"accepted": 1, "duplicates": 0, "skipped": []})
        sync.push(db, t)
        self.assertEqual([a["key"] for a in t.calls[0][2]["attempts"]], ["bug:b"])

    def test_batches_are_sent_in_order_and_skipped_rows_are_not_resent_in_one_run(self):
        db = kata.connect()
        dev = kata.device_id(db)
        for i in range(5):
            key = "kata:skip" if i == 1 else "kata:a"
            self.insert(db, key, f"2026-11-0{i + 1}T09:00:00+00:00", 5, 1, 1, f"{dev}:{i + 1}")

        def answer(method, path, body):
            keys = {a["key"] for a in body["attempts"]}
            return {"accepted": len(body["attempts"]), "duplicates": 0,
                    "skipped": ["kata:skip"] if "kata:skip" in keys else []}

        with mock.patch.object(sync, "BATCH", 2):
            t = Fake(answer, answer, answer)
            out = sync.push(db, t)
        self.assertEqual([len(c[2]["attempts"]) for c in t.calls], [2, 2, 1])
        self.assertEqual(out["skipped"], {"kata:skip"})
        left = db.execute("SELECT key FROM ex_attempts WHERE pushed = 0").fetchall()
        self.assertEqual([r["key"] for r in left], ["kata:skip"])


class PullTest(Base):
    def web(self, i, key="kata:a", ts="2026-11-02T09:00:00Z", q=5.0, passed=None, total=None,
            uid=None):
        return {"id": i, "uid": uid or f"web:{i}", "key": key, "ts": ts, "q": q,
                "passed": passed, "total": total}

    def test_pull_inserts_dedupes_advances_the_cursor_and_replays(self):
        db = kata.connect()
        dev = kata.device_id(db)
        kata.record(db, "kata:a", 5, 15, 15)  # a local attempt that was already pushed
        mine = db.execute("SELECT uid, ts FROM ex_attempts").fetchone()
        db.execute("UPDATE ex_attempts SET pushed = 1")
        rows = [
            self.web(10, uid=mine["uid"], ts=sync.to_z(mine["ts"]), passed=15, total=15),  # echo
            self.web(11, key="bug:b", ts="2000-01-01T00:00:00Z", q=4.2),  # older than everything
            self.web(12, ts="2026-11-03T09:00:00Z", q=3.0, passed=12, total=15),
        ]
        t = Fake({"attempts": rows, "next": 12})
        new, touched = sync.pull(db, t)
        self.assertEqual((new, touched), (2, {"bug:b", "kata:a"}))
        self.assertEqual(t.calls[0][1], "/api/sync/pull?after=0&limit=500")
        self.assertEqual(sync.get_state(db, "pull_cursor"), "12")
        got = db.execute("SELECT key, ts, passed, total, q, pushed FROM ex_attempts "
                         "WHERE uid LIKE 'web:%' ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in got], [
            ("bug:b", "2000-01-01T00:00:00+00:00", 4, 5, 4.2, 1),  # counts derived from q
            ("kata:a", "2026-11-03T09:00:00+00:00", 12, 15, 3.0, 1),
        ])
        self.assertEqual(sync.push(db, Fake())["pushed"], 0)  # pulled rows are never pushed back
        self.assertEqual(dev, kata.device_id(db))

    def test_full_sync_rebuilds_state_and_resumes_from_the_cursor(self):
        db = kata.connect()
        pulled = {"attempts": [self.web(5, ts="2026-11-01T09:00:00Z", q=1.0)], "next": 5}
        stats = sync.sync(db, Fake(pulled))  # nothing to push, so the only call is the pull
        self.assertEqual(stats, {"pushed": 0, "duplicates": 0, "skipped": [], "pulled": 1})
        state = db.execute("SELECT reps, interval FROM ex_state WHERE key = 'kata:a'").fetchone()
        self.assertEqual((state["reps"], state["interval"]), (0, 1.0))  # a failing attempt
        t = Fake({"attempts": [], "next": 5})
        sync.sync(db, t)
        self.assertEqual(t.calls[0][1], "/api/sync/pull?after=5&limit=500")

    def test_a_cursor_that_does_not_advance_is_an_error(self):
        db = kata.connect()
        with self.assertRaises(sync.SyncError):
            sync.pull(db, Fake({"attempts": [self.web(1)], "next": 0}))

    def test_two_devices_converge(self):
        """Both devices end with the same schedule although they synced in different orders."""
        server = {"attempts": []}  # a minimal stand-in for the Worker's event log

        def transport_for(db):
            def call(method, path, body=None):
                if method == "POST":
                    seen = {a["uid"] for a in server["attempts"]}
                    new = [a for a in body["attempts"] if a["uid"] not in seen]
                    for a in new:
                        server["attempts"].append({**a, "id": len(server["attempts"]) + 1})
                    return {"accepted": len(new), "duplicates": len(body["attempts"]) - len(new),
                            "skipped": []}
                after = int(path.split("after=")[1].split("&")[0])
                rows = [a for a in server["attempts"] if a["id"] > after]
                return {"attempts": rows, "next": rows[-1]["id"] if rows else after}
            return call

        dev_a, dev_b = Path(self.root / "a"), Path(self.root / "b")
        dbs = []
        for d in (dev_a, dev_b):
            with mock.patch.object(drill, "HOME", d):
                dbs.append(kata.connect())
        a, b = dbs
        self.insert(a, "kata:k", "2026-11-01T09:00:00+00:00", 5, 1, 1, f"{kata.device_id(a)}:1")
        self.insert(b, "kata:k", "2026-11-03T09:00:00+00:00", 3, 4, 5, f"{kata.device_id(b)}:1")
        self.insert(a, "kata:k", "2026-11-05T09:00:00+00:00", 5, 1, 1, f"{kata.device_id(a)}:2")
        sync.sync(a, transport_for(a))
        sync.sync(b, transport_for(b))
        sync.sync(a, transport_for(a))  # a picks up b's attempt
        sync.sync(b, transport_for(b))
        def read(db):
            row = db.execute("SELECT ease, interval, reps, due FROM ex_state "
                             "WHERE key = 'kata:k'").fetchone()
            return tuple(row)

        self.assertEqual(read(a), read(b))
        self.assertEqual(a.execute("SELECT COUNT(*) FROM ex_attempts").fetchone()[0], 3)
        self.assertEqual(b.execute("SELECT COUNT(*) FROM ex_attempts").fetchone()[0], 3)


class Handler(BaseHTTPRequestHandler):
    log: list = []
    reply = (200, "application/json", b'{"ok": true}')

    def do_GET(self):
        self.handle_any()

    def do_POST(self):
        self.handle_any()

    def handle_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        Handler.log.append((self.command, self.path, dict(self.headers), self.rfile.read(n)))
        status, ctype, body = Handler.reply
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.log = []
        Handler.reply = (200, "application/json", b'{"ok": true}')

    def test_sends_json_with_access_service_token_headers(self):
        out = sync.Http(self.url, "id.access", "secret")("POST", "/api/sync/push", {"a": 1})
        self.assertEqual(out, {"ok": True})
        (method, path, headers, body), = Handler.log
        headers = {k.lower(): v for k, v in headers.items()}  # urllib capitalizes names
        self.assertEqual((method, path, json.loads(body)), ("POST", "/api/sync/push", {"a": 1}))
        self.assertEqual(headers["cf-access-client-id"], "id.access")
        self.assertEqual(headers["cf-access-client-secret"], "secret")
        self.assertEqual(headers["x-drill"], "1")
        self.assertEqual(headers["content-type"], "application/json")

    def test_no_token_headers_for_local_development(self):
        sync.Http(self.url + "/")("GET", "/api/sync/pull?after=0")
        (_, path, headers, _), = Handler.log
        self.assertEqual(path, "/api/sync/pull?after=0")
        self.assertNotIn("cf-access-client-id", {k.lower() for k in headers})

    def test_errors_are_explained(self):
        Handler.reply = (403, "application/json", b'{"error": "forbidden"}')
        with self.assertRaisesRegex(sync.SyncError, r"HTTP 403: .*forbidden"):
            sync.Http(self.url)("GET", "/x")
        Handler.reply = (200, "text/html", b"<html>Sign in</html>")  # an Access login page
        with self.assertRaisesRegex(sync.SyncError, "not JSON.*Access policy"):
            sync.Http(self.url)("GET", "/x")
        with self.assertRaisesRegex(sync.SyncError, "cannot reach"):
            sync.Http("http://127.0.0.1:9", timeout=2)("GET", "/x")

    def test_environment_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DRILL_URL", None)
            with self.assertRaisesRegex(sync.SyncError, "DRILL_URL"):
                sync.transport_from_env()
        env = {"DRILL_URL": "https://x.example/", "DRILL_CLIENT_ID": "i",
               "DRILL_CLIENT_SECRET": "s"}
        with mock.patch.dict(os.environ, env):
            t = sync.transport_from_env()
        self.assertEqual((t.url, t.client_id, t.secret), ("https://x.example", "i", "s"))


class AutosyncTest(Base):
    def run_autosync(self, env, transport=None):
        db = kata.connect()
        out = io.StringIO()
        patches = [mock.patch.dict(os.environ, env)]
        if transport:
            patches.append(mock.patch.object(sync, "transport_from_env", lambda: transport))
        with contextlib.redirect_stdout(out), contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            kata.autosync(db)
        return out.getvalue()

    def test_no_url_means_silence(self):
        os.environ.pop("DRILL_URL", None)
        self.assertEqual(self.run_autosync({}), "")

    def test_success_and_failure_never_raise(self):
        ok = Fake({"attempts": [], "next": 0})
        self.assertEqual(self.run_autosync({"DRILL_URL": "http://x"}, ok),
                         "synced: pushed 0, pulled 0\n")

        def boom(*a):
            raise sync.SyncError("cannot reach http://x")

        self.assertEqual(self.run_autosync({"DRILL_URL": "http://x"}, boom),
                         "sync skipped: cannot reach http://x\n")


def fixture_payload(root: Path) -> dict:
    """Deterministic events, deliberately out of time order, with their replayed schedules."""
    with mock.patch.object(drill, "HOME", root / "fixture-home"), \
            mock.patch.object(drill, "CARDS", root / "cards"):
        db = kata.connect()
        events = [  # (key, ts, q, passed, total) in insertion order
            ("kata:fixture-a", "2026-11-01T09:00:00+00:00", 1, 3, 15),
            ("kata:fixture-a", "2026-11-05T09:00:00+00:00", 3, 12, 15),
            ("kata:fixture-a", "2026-11-02T09:00:00+00:00", 5, 15, 15),
            ("bug:fixture-b", "2026-11-03T10:00:00+00:00", 4.2, 4, 5),
            ("bug:fixture-b", "2026-11-03T10:00:00+00:00", 2.5, 2, 5),  # same second: id order
            ("bug:fixture-b", "2026-11-09T10:00:00+00:00", 5.0, 5, 5),
        ]
        for i, (key, ts, q, passed, total) in enumerate(events, 1):
            db.execute("INSERT INTO ex_attempts(key, ts, passed, total, detail, q, uid, pushed) "
                       "VALUES(?,?,?,?,?,?,?,0)", (key, ts, passed, total, "", q, f"fixture:{i}"))
        db.commit()
        rows = db.execute("SELECT * FROM ex_attempts ORDER BY id").fetchall()
        expected = {}
        for key in ("kata:fixture-a", "bug:fixture-b"):
            sync.replay_local(db, key)
            expected[key] = dict(db.execute("SELECT ease, interval, reps, due FROM ex_state "
                                            "WHERE key = ?", (key,)).fetchone())
            expected[key]["due"] = sync.to_z(expected[key]["due"])
        return {"attempts": [sync.attempt_json(r) for r in rows], "expected": expected}


class FixtureTest(Base):
    def test_cross_language_fixture_is_current(self):
        """The Worker's tests replay this file; regenerate with UPDATE_FIXTURE=1 if it changes."""
        payload = fixture_payload(self.root)
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if os.environ.get("UPDATE_FIXTURE"):
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(text)
        self.assertEqual(FIXTURE.read_text(), text, "run: UPDATE_FIXTURE=1 python3 test_sync.py")


if __name__ == "__main__":
    unittest.main()
