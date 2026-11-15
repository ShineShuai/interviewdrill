import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { beforeEach, test } from "node:test";
import type { Env } from "../src/env.ts";
import { handle } from "../src/index.ts";
import { replay } from "../src/srs.ts";
import { fakeD1 } from "./fake-d1.ts";

// Produced by test_sync.py (UPDATE_FIXTURE=1): events out of time order, with the schedules
// that the Python implementation of the replay computed for them.
interface Wire {
  uid: string; key: string; ts: string; q: number; passed: number | null; total: number | null;
  secs: number;
}
interface Expected { ease: number; interval: number; reps: number; due: string }
const fixture = JSON.parse(
  readFileSync(new URL("./fixtures/sync-push.json", import.meta.url), "utf8"),
) as { attempts: Wire[]; expected: Record<string, Expected> };

let db: ReturnType<typeof fakeD1>;
let env: Env;

beforeEach(() => {
  db = fakeD1();
  db.raw.exec(
    "INSERT INTO cards VALUES('kata:fixture-a','katas','Q','- r')," +
      "('bug:fixture-b','bugs','Q','- r'),('go-chan','go','Q','- r')",
  );
  env = { DB: db as unknown as Env["DB"], ANTHROPIC_API_KEY: "k", DEV_USER: "me" } as Env;
});

const call = (path: string, init: RequestInit = {}) =>
  handle(
    new Request(`http://localhost${path}`, {
      ...init,
      headers: { "X-Drill": "1", "Content-Type": "application/json" },
    }),
    env,
  );
const push = (attempts: unknown) =>
  call("/api/sync/push", { method: "POST", body: JSON.stringify({ attempts }) });
const state = (card: string, user = "me") =>
  db.raw
    .prepare("SELECT ease, interval, reps, due FROM card_state WHERE user_id = ? AND card_id = ?")
    .get(user, card) as unknown as Expected | undefined;

test("pushing the Python fixture reproduces the Python schedules exactly", async () => {
  const res = await push(fixture.attempts);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { accepted: 6, duplicates: 0, skipped: [] });
  for (const [key, want] of Object.entries(fixture.expected)) {
    const got = state(key);
    assert.ok(got, key);
    assert.ok(Math.abs(got.ease - want.ease) < 1e-12, `${key} ease ${got.ease} vs ${want.ease}`);
    assert.ok(Math.abs(got.interval - want.interval) < 1e-12, `${key} interval`);
    assert.equal(got.reps, want.reps, `${key} reps`);
    const dueGap = Math.abs(Date.parse(got.due) - Date.parse(want.due));
    assert.ok(dueGap <= 1000, `${key} due ${got.due} vs ${want.due}`);
  }
});

test("pushing again is idempotent", async () => {
  await push(fixture.attempts);
  const before = state("kata:fixture-a");
  const res = await push(fixture.attempts);
  assert.deepEqual(await res.json(), { accepted: 0, duplicates: 6, skipped: [] });
  assert.deepEqual(state("kata:fixture-a"), before);
  const n = db.raw.prepare("SELECT COUNT(*) AS n FROM attempts").get() as { n: number };
  assert.equal(n.n, 6);
});

test("attempts for cards the web does not have are skipped, the rest is accepted", async () => {
  const unknown = { ...fixture.attempts[0], uid: "x:1", key: "kata:not-published" };
  const res = await push([unknown, fixture.attempts[0]]);
  assert.deepEqual(await res.json(), {
    accepted: 1, duplicates: 0, skipped: ["kata:not-published"],
  });
  assert.equal(state("kata:not-published"), undefined);
});

test("the schedule is the replay of all attempts by time, web and CLI together", async () => {
  db.raw.prepare("INSERT INTO cards VALUES('kata:mixed','katas','Q','- r')").run();
  const web = await call("/api/kata/result", {
    method: "POST", body: JSON.stringify({ card_id: "kata:mixed", passed: 15, total: 15 }),
  });
  assert.equal(web.status, 200);
  const event = (n: number, q: number, passed: number) => ({
    uid: `d:${n}`, key: "kata:mixed", ts: `2026-01-0${n}T09:00:00Z`, q, passed, total: 15, secs: 0,
  });
  const older = [event(1, 1, 3), event(2, 5, 15)];
  await push(older);
  const rows = db.raw
    .prepare("SELECT ts, score AS q FROM attempts WHERE card_id = 'kata:mixed' ORDER BY ts, id")
    .all() as unknown as { ts: string; q: number }[];
  assert.equal(rows.length, 3);
  assert.equal(rows[2].q, 5); // the web attempt happened last
  const want = replay(rows);
  const got = state("kata:mixed");
  const pick = (s?: { ease: number; interval: number; reps: number }) =>
    [s?.ease, s?.interval, s?.reps];
  assert.deepEqual(pick(got), pick(want ?? undefined));
});

test("push validates every field", async () => {
  const ok = fixture.attempts[0];
  const bad = async (patch: object, needle: string | RegExp) => {
    const res = await push([ok, { ...ok, uid: "v:1", ...patch }]);
    assert.equal(res.status, 400, JSON.stringify(patch));
    assert.match(((await res.json()) as { error: string }).error, needle);
  };
  await bad({ uid: "" }, /attempts\[1\]: bad uid/);
  await bad({ uid: "x".repeat(81) }, /bad uid/);
  await bad({ key: "go-chan" }, /key must be kata:\* or bug:\*/);
  await bad({ ts: "2026-11-01T09:00:00+00:00" }, /ts must be UTC/);
  await bad({ ts: "2026-11-01 09:00:00" }, /ts must be UTC/);
  await bad({ q: 5.5 }, /q must be 0\.\.5/);
  await bad({ q: "5" }, /q must be 0\.\.5/);
  await bad({ passed: 3, total: 2 }, /bad counts/);
  await bad({ passed: 1.5, total: 2 }, /bad counts/);
  await bad({ passed: 1, total: 5000 }, /bad counts/);
  assert.equal((await push("nope")).status, 400);
  assert.equal((await push(Array(201).fill(ok))).status, 400);
  assert.equal((await push([null])).status, 400);
  const n = db.raw.prepare("SELECT COUNT(*) AS n FROM attempts").get() as { n: number };
  assert.equal(n.n, 0); // a bad request stores nothing, not even its valid items
});

test("pull returns kata and bug attempts of this user in id order, with a cursor", async () => {
  await push(fixture.attempts.slice(0, 2));
  await call("/api/kata/result", {
    method: "POST", body: JSON.stringify({ card_id: "kata:fixture-a", passed: 4, total: 5 }),
  });
  db.raw.prepare(
    "INSERT INTO attempts(user_id, card_id, ts, score, uid) VALUES" +
      "('me','go-chan','2026-11-01T00:00:00Z',5,NULL)," +
      "('other','kata:fixture-a','2026-11-01T00:00:00Z',5,NULL)",
  ).run();
  type Pulled = { id: number; uid: string; key: string; ts: string; q: number;
                  passed: number | null; total: number | null };
  const pull = async (query = "") =>
    (await (await call(`/api/sync/pull${query}`)).json()) as { attempts: Pulled[]; next: number };
  const all = await pull();
  assert.deepEqual(all.attempts.map((a) => a.id), [1, 2, 3]);
  assert.deepEqual(all.attempts[0], {
    id: 1, uid: fixture.attempts[0].uid, key: "kata:fixture-a", ts: fixture.attempts[0].ts,
    q: fixture.attempts[0].q, passed: 3, total: 15,
  });
  assert.equal(all.attempts[2].uid, "web:3"); // web attempts get a stable derived uid
  assert.deepEqual([all.attempts[2].passed, all.attempts[2].total], [4, 5]);
  assert.equal(all.next, 3);
  const rest = await pull("?after=1&limit=1");
  assert.deepEqual(rest.attempts.map((a) => a.id), [2]);
  assert.equal(rest.next, 2);
  const none = await pull("?after=3");
  assert.deepEqual(none, { attempts: [], next: 3 });
});

test("pull keeps passed/total null for graded answers without test counts", async () => {
  db.raw.prepare(
    "INSERT INTO attempts(user_id, card_id, ts, score, detail, uid) VALUES" +
      "('me','bug:fixture-b','2026-11-01T00:00:00Z',3.5,'{\"correct\":4}',NULL)," +
      "('me','bug:fixture-b','2026-11-02T00:00:00Z',2,'not json',NULL)",
  ).run();
  const res = await call("/api/sync/pull");
  const out = (await res.json()) as { attempts: { passed: unknown; total: unknown }[] };
  assert.deepEqual(out.attempts.map((a) => [a.passed, a.total]), [[null, null], [null, null]]);
});

test("users do not see each other's attempts", async () => {
  await push(fixture.attempts);
  env.DEV_USER = "other";
  const out = (await (await call("/api/sync/pull")).json()) as { attempts: unknown[] };
  assert.deepEqual(out.attempts, []);
  assert.equal(state("kata:fixture-a", "other"), undefined);
});
