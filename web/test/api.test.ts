import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";
import type { Env } from "../src/env.ts";
import { handle } from "../src/index.ts";
import { fakeD1 } from "./fake-d1.ts";

const real = globalThis.fetch;
let db: ReturnType<typeof fakeD1>;
let env: Env;
let sent: { url: string; body: Record<string, unknown> }[];
let gradeScores = { correct: 5, depth: 5, clarity: 5 };

const stubGrader = (async (url: string, init: RequestInit) => {
  sent.push({ url, body: JSON.parse(String(init.body)) });
  const input = { ...gradeScores, missed: ["m"], errors: [], followup: "f?" };
  return Response.json({ content: [{ type: "tool_use", name: "grade", input }] });
}) as unknown as typeof fetch;

beforeEach(() => {
  db = fakeD1();
  db.raw.exec(
    "INSERT INTO cards VALUES('c1','go','Q one?','- ref one'),('c2','go','Q two?','- ref two')," +
      "('c3','c++','Q three?','- ref three')",
  );
  env = {
    DB: db as unknown as Env["DB"],
    AI: { run: async () => ({ text: " hello " }) } as unknown as Env["AI"],
    ANTHROPIC_API_KEY: "key",
    LLM: "anthropic", // these tests stub Anthropic; Workers AI is tested in grader.test.ts
    DEV_USER: "me",
    DAILY_CAP: "50",
  };
  sent = [];
  gradeScores = { correct: 5, depth: 5, clarity: 5 };
  globalThis.fetch = stubGrader;
});
after(() => {
  globalThis.fetch = real;
});

const call = (path: string, init: RequestInit & { host?: string } = {}) =>
  handle(
    new Request(`http://${init.host ?? "localhost"}${path}`, {
      ...init,
      headers: { "X-Drill": "1", "Content-Type": "application/json", ...init.headers },
    }),
    env,
  );
const post = (path: string, body: unknown) =>
  call(path, { method: "POST", body: JSON.stringify(body) });

test("auth: unconfigured 500, no token 401, no CSRF header 403", async () => {
  assert.equal((await call("/api/status", { host: "app.example.com" })).status, 500);
  env.TEAM_DOMAIN = "https://team.cloudflareaccess.com";
  env.ACCESS_AUD = "aud";
  assert.equal((await call("/api/status", { host: "app.example.com" })).status, 401);
  const bare = await handle(new Request("http://localhost/api/attempt", { method: "POST" }), env);
  assert.equal(bare.status, 403);
  assert.equal((await call("/api/nope")).status, 404);
});

test("next never leaks the reference and honors topic and n", async () => {
  const all = (await (await call("/api/next?n=10")).json()) as { cards: object[] };
  assert.equal(all.cards.length, 3);
  assert.ok(all.cards.every((c) => !("ref" in c)));
  const res = await call("/api/next?topic=c%2B%2B");
  const cpp = (await res.json()) as { cards: { id: string }[] };
  assert.deepEqual(cpp.cards.map((c) => c.id), ["c3"]);
  const one = (await (await call("/api/next?n=1")).json()) as { cards: object[] };
  assert.equal(one.cards.length, 1);
});

test("attempt: grades, schedules, stores, hides card from due list", async () => {
  const res = await post("/api/attempt", { card_id: "c1", answer: "my answer", secs: 12 });
  assert.equal(res.status, 200);
  const out = (await res.json()) as { score: number; interval: number; ref: string };
  assert.equal(out.score, 5);
  assert.equal(out.interval, 1);
  assert.equal(out.ref, "- ref one");
  assert.equal(sent[0].url, "https://api.anthropic.com/v1/messages");
  assert.deepEqual(sent[0].body.tool_choice, { type: "tool", name: "grade" });
  assert.match(String((sent[0].body.messages as { content: string }[])[0].content), /my answer/);

  const st = (await (await call("/api/status")).json()) as {
    due: number;
    today: { n: number };
    streak: number;
  };
  assert.deepEqual([st.due, st.today.n, st.streak], [2, 1, 1]);
  const state = db.raw.prepare("SELECT reps, interval FROM card_state WHERE card_id='c1'").get();
  assert.deepEqual({ ...state }, { reps: 1, interval: 1 });
});

test("attempt: failing grade resets reps; repeat attempts upsert one state row", async () => {
  await post("/api/attempt", { card_id: "c1", answer: "a" });
  gradeScores = { correct: 0, depth: 0, clarity: 0 };
  await post("/api/attempt", { card_id: "c1", answer: "b" });
  const rows = db.raw.prepare("SELECT reps FROM card_state WHERE card_id='c1'").all();
  assert.deepEqual(rows.map((r) => ({ ...r })), [{ reps: 0 }]);
  const n = db.raw.prepare("SELECT COUNT(*) AS n FROM attempts").get() as { n: number };
  assert.equal(n.n, 2);
});

test("attempt: validation and quota", async () => {
  assert.equal((await post("/api/attempt", { card_id: "c1", answer: "  " })).status, 400);
  assert.equal((await post("/api/attempt", { card_id: "zz", answer: "a" })).status, 404);
  const long = "x".repeat(4001);
  assert.equal((await post("/api/attempt", { card_id: "c1", answer: long })).status, 413);
  assert.equal((await call("/api/attempt", { method: "POST", body: "[" })).status, 400);
  assert.equal(sent.length, 0);
  env.DAILY_CAP = "2";
  assert.equal((await post("/api/attempt", { card_id: "c1", answer: "a" })).status, 200);
  assert.equal((await post("/api/attempt", { card_id: "c2", answer: "a" })).status, 200);
  assert.equal((await post("/api/attempt", { card_id: "c3", answer: "a" })).status, 429);
  assert.equal(sent.length, 2);
});

test("exercise cards: grader gets the kind hint and multi-line question round-trips", async () => {
  const code = "Find the bug.\n\n  1 | int main() {}";
  db.raw
    .prepare("INSERT INTO cards VALUES('bug:uaf', 'bugs', ?, '- Bug class: heap-use-after-free')")
    .run(code);
  const res0 = await call("/api/next?topic=bugs");
  const next = (await res0.json()) as { cards: { question: string }[] };
  assert.equal(next.cards[0].question, code);
  const topics = async () => {
    const st = (await (await call("/api/status")).json()) as { topics: { topic: string }[] };
    return st.topics.map((t) => t.topic);
  };
  assert.ok((await topics()).includes("bugs"));
  const res = await post("/api/attempt", { card_id: "bug:uaf", answer: "use after free" });
  assert.equal(res.status, 200);
  assert.match(String(sent[0].body.system), /debugging exercise/);
  assert.ok(!(await topics()).includes("bugs")); // answered, so no longer due
});

test("problem result from the browser runner: graded by pass ratio, no LLM involved", async () => {
  db.raw
    .prepare("INSERT INTO cards VALUES('problem:max', 'problems', 'Problem: x', '- ref')")
    .run();
  const result = async (passed: number, total: number) => {
    const body = { card_id: "problem:max", passed, total, secs: 40 };
    const res = await post("/api/problem/result", body);
    assert.equal(res.status, 200);
    return (await res.json()) as { score: number; interval: number };
  };
  assert.deepEqual(await result(15, 15), { score: 5, interval: 1, passed: 15, total: 15 });
  assert.deepEqual(await result(12, 15), { score: 3, interval: 3, passed: 12, total: 15 });
  assert.deepEqual(await result(3, 15), { score: 1, interval: 1, passed: 3, total: 15 });
  const all = db.raw.prepare("SELECT * FROM attempts ORDER BY id").all();
  const rows = all as Record<string, unknown>[];
  assert.equal(rows.length, 3);
  assert.deepEqual(
    [rows[2].answer, rows[2].correct, rows[2].depth, rows[2].score, rows[2].secs],
    ["(browser tests)", 1, null, 1, 40],
  );
  assert.deepEqual(JSON.parse(String(rows[2].detail)), { mode: "browser", passed: 3, total: 15 });
  assert.equal(sent.length, 0);
  const state = db.raw.prepare("SELECT reps FROM card_state WHERE card_id = 'problem:max'").get();
  assert.deepEqual({ ...state }, { reps: 0 }); // the failing run reset the card
});

test("problem result validates its input", async () => {
  db.raw
    .prepare("INSERT INTO cards VALUES('problem:max', 'problems', 'Problem: x', '- ref')")
    .run();
  const status = async (body: object) => (await post("/api/problem/result", body)).status;
  assert.equal(await status({ card_id: "go-chan", passed: 1, total: 1 }), 400); // not a problem
  assert.equal(await status({ card_id: "problem:max", passed: 1, total: 0 }), 400);
  assert.equal(await status({ card_id: "problem:max", passed: 5, total: 4 }), 400);
  assert.equal(await status({ card_id: "problem:max", passed: -1, total: 4 }), 400);
  assert.equal(await status({ card_id: "problem:max", passed: "x", total: 4 }), 400);
  assert.equal(await status({ card_id: "problem:max", passed: 1, total: 5000 }), 400);
  assert.equal(await status({ card_id: "problem:nope", passed: 1, total: 1 }), 404);
  assert.equal((await call("/api/problem/result", { method: "POST", body: "[" })).status, 400);
});

test("grader failure maps to 502 and stores nothing", async () => {
  globalThis.fetch = (async () => new Response("boom", { status: 500 })) as typeof fetch;
  assert.equal((await post("/api/attempt", { card_id: "c1", answer: "a" })).status, 502);
  const n = db.raw.prepare("SELECT COUNT(*) AS n FROM attempts").get() as { n: number };
  assert.equal(n.n, 0);
});

test("users are isolated", async () => {
  await post("/api/attempt", { card_id: "c1", answer: "a" });
  env.DEV_USER = "other";
  const st = (await (await call("/api/status")).json()) as { due: number; today: { n: number } };
  assert.deepEqual([st.due, st.today.n], [3, 0]);
  const ex = (await (await call("/api/export")).json()) as { attempts: unknown[] };
  assert.equal(ex.attempts.length, 0);
});

test("report and export", async () => {
  await post("/api/attempt", { card_id: "c1", answer: "a" });
  gradeScores = { correct: 1, depth: 1, clarity: 1 };
  await post("/api/attempt", { card_id: "c3", answer: "a" });
  const r = (await (await call("/api/report")).json()) as {
    topics: { topic: string; n: number }[];
    weakest: { card_id: string; question: string }[];
    trend: { n: number }[];
  };
  assert.deepEqual(r.topics.map((t) => t.topic), ["c++", "go"]); // ordered weakest first
  assert.equal(r.weakest[0].card_id, "c3");
  assert.equal(r.weakest[0].question, "Q three?");
  assert.equal(r.trend[0].n, 2);
  const res = await call("/api/export");
  assert.match(res.headers.get("content-disposition") ?? "", /drill-export\.json/);
  const ex = (await res.json()) as { attempts: unknown[]; card_state: unknown[] };
  assert.deepEqual([ex.attempts.length, ex.card_state.length], [2, 2]);
});

test("stt transcribes and enforces size", async () => {
  const ok = await call("/api/stt", { method: "POST", body: new Uint8Array(100) });
  assert.deepEqual(await ok.json(), { text: "hello" });
  assert.equal((await call("/api/stt", { method: "POST", body: new Uint8Array(0) })).status, 413);
  env.AI = { run: async () => { throw new Error("x"); } } as unknown as Env["AI"];
  assert.equal((await call("/api/stt", { method: "POST", body: new Uint8Array(10) })).status, 502);
});
