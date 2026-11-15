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
