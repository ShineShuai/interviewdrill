import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";
import { checklist } from "../src/checklist.ts";
import type { Env } from "../src/env.ts";
import { handle } from "../src/index.ts";
import { callJson, extractJson, providerOf, type JsonRequest } from "../src/llm.ts";
import { fakeD1 } from "./fake-d1.ts";

const GRADE = { correct: 4, depth: 3, clarity: 5, missed: ["m"], errors: [], followup: "f?" };
const REQ: JsonRequest = {
  system: "be strict", prompt: "the prompt", maxTokens: 321,
  tool: { name: "grade", description: "d", input_schema: { type: "object", properties: {} } },
};

type Run = (model: string, input: Record<string, unknown>) => Promise<unknown>;
const calls: { model: string; input: Record<string, unknown> }[] = [];
let script: Run;
const ai = {
  run: async (model: string, input: Record<string, unknown>) => {
    calls.push({ model, input });
    return script(model, input);
  },
};
const envOf = (extra: Partial<Env> = {}): Env => ({ AI: ai, ...extra }) as unknown as Env;

const realFetch = globalThis.fetch;
after(() => {
  globalThis.fetch = realFetch;
});
beforeEach(() => {
  calls.length = 0;
  script = async () => ({ response: GRADE });
});

test("providerOf follows LLM and what is configured", () => {
  assert.equal(providerOf(envOf()), "workers-ai"); // the default needs no key
  assert.equal(providerOf(envOf({ LLM: " Workers-AI " })), "workers-ai");
  assert.equal(providerOf({} as Env), "none"); // no AI binding
  assert.equal(providerOf(envOf({ LLM: "none" })), "none");
  assert.equal(providerOf(envOf({ LLM: "anthropic" })), "none"); // no key
  assert.equal(providerOf(envOf({ LLM: "anthropic", ANTHROPIC_API_KEY: "k" })), "anthropic");
  assert.equal(providerOf(envOf({ LLM: "gpt" })), "none");
});

test("extractJson tolerates fences and prose, rejects everything else", () => {
  assert.deepEqual(extractJson('{"a": 1}'), { a: 1 });
  assert.deepEqual(extractJson('ok:\n```json\n{"a": {"b": 2}}\n```'), { a: { b: 2 } });
  assert.deepEqual(extractJson('Sure {"a": 1} done'), { a: 1 });
  assert.throws(() => extractJson("prose"), SyntaxError);
  assert.throws(() => extractJson("[1, 2]"), /not a JSON object/);
  assert.throws(() => extractJson('{"a": '), SyntaxError);
});

test("Workers AI: JSON-mode request, first model, object reply", async () => {
  assert.deepEqual(await callJson(envOf(), REQ), GRADE);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].model, "@cf/meta/llama-3.3-70b-instruct-fp8-fast");
  assert.deepEqual(calls[0].input.messages, [
    { role: "system", content: "be strict" }, { role: "user", content: "the prompt" },
  ]);
  assert.deepEqual(calls[0].input.response_format, {
    type: "json_schema", json_schema: REQ.tool.input_schema,
  });
  assert.equal(calls[0].input.max_tokens, 321);
  assert.equal(calls[0].input.temperature, 0);
});

test("Workers AI: a reply given as text is parsed too", async () => {
  script = async () => ({ response: `Here:\n\`\`\`json\n${JSON.stringify(GRADE)}\n\`\`\`` });
  assert.deepEqual(await callJson(envOf(), REQ), GRADE);
});

test("Workers AI: falls back to the next model when one is refused", async () => {
  script = async (model) => {
    if (model.includes("70b")) throw new Error("5016: model requires the Workers Paid plan");
    return { response: GRADE };
  };
  assert.deepEqual(await callJson(envOf(), REQ), GRADE);
  assert.deepEqual(calls.map((c) => c.model), [
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/meta/llama-3.1-8b-instruct-fast",
  ]);
  calls.length = 0;
  await callJson(envOf({ LLM_MODEL: " @cf/a/one , @cf/b/two " }), REQ);
  assert.deepEqual(calls.map((c) => c.model), ["@cf/a/one"]); // custom list, first one works
});

test("Workers AI: a model that refuses JSON mode is asked again without it", async () => {
  script = async (_model, input) => {
    if (input.response_format) throw new Error("8001: JSON Mode couldn't be met");
    return { response: `Sure:\n${JSON.stringify(GRADE)}` };
  };
  assert.deepEqual(await callJson(envOf(), REQ), GRADE);
  assert.deepEqual(calls.map((c) => [c.model, "response_format" in c.input]), [
    ["@cf/meta/llama-3.3-70b-instruct-fp8-fast", true],
    ["@cf/meta/llama-3.3-70b-instruct-fp8-fast", false], // same model, schema in the prompt
  ]);
  const system = (calls[1].input.messages as { content: string }[])[0].content;
  assert.match(system, /^be strict\nReply with only a JSON object/);
  assert.match(system, /"type":"object"/);
  assert.equal(calls[1].input.temperature, 0);
  calls.length = 0;
  script = async () => {
    throw new Error("JSON Mode couldn't be met");
  };
  await assert.rejects(callJson(envOf(), REQ), { status: 502 });
  assert.equal(calls.length, 4); // two models, two modes each; no endless retry
});

test("Workers AI: every model failing is a 502 the app can answer with self-grading", async () => {
  script = async () => {
    throw new Error("daily allowance used up");
  };
  await assert.rejects(callJson(envOf(), REQ), { status: 502, message: "grader unavailable" });
  assert.equal(calls.length, 2);
});

test("Workers AI: an unusable reply is asked for once more, then a 502", async () => {
  let n = 0;
  script = async () => ({ response: n++ === 0 ? "I cannot do that" : GRADE });
  assert.deepEqual(await callJson(envOf(), REQ), GRADE);
  assert.equal(calls.length, 2);
  calls.length = 0;
  script = async () => ({ response: "still prose" });
  await assert.rejects(callJson(envOf(), REQ), { status: 502 });
  assert.equal(calls.length, 2);
});

test("Anthropic path: forced tool call with the configured model", async () => {
  const sent: { headers: Record<string, string>; body: Record<string, unknown> }[] = [];
  globalThis.fetch = (async (_url: string, init: RequestInit) => {
    const headers = init.headers as Record<string, string>;
    sent.push({ headers, body: JSON.parse(String(init.body)) });
    return Response.json({ content: [{ type: "tool_use", input: GRADE }] });
  }) as unknown as typeof fetch;
  const env = envOf({ LLM: "anthropic", ANTHROPIC_API_KEY: "sk", ANTHROPIC_MODEL: "claude-x" });
  assert.deepEqual(await callJson(env, REQ), GRADE);
  assert.equal(sent[0].headers["x-api-key"], "sk");
  assert.equal(sent[0].body.model, "claude-x");
  assert.deepEqual(sent[0].body.tool_choice, { type: "tool", name: "grade" });
  assert.equal(calls.length, 0); // Workers AI untouched
});

test("no grader: 409 without calling anything", async () => {
  await assert.rejects(callJson(envOf({ LLM: "none" }), REQ), { status: 409 });
  assert.equal(calls.length, 0);
});

test("checklist: bullets for cards, fixed lists for exercises", () => {
  assert.deepEqual(checklist("go-chan", "- a\n- b\ntext\n-c\n- "), ["a", "b"]);
  assert.deepEqual(checklist("go-x", "  plain text "), ["plain text"]);
  assert.equal(checklist("bug:x", "- Bug class: x\n-    int* p;").length, 3);
  assert.equal(checklist("problem:x", "- Intended approach: y").length, 4);
  assert.equal(checklist("bugfix", "- a")[0], "a"); // the prefix needs the colon
});

// --- through the API ---
let db: ReturnType<typeof fakeD1>;
let env: Env;

beforeEach(() => {
  db = fakeD1();
  db.raw.exec(
    "INSERT INTO cards VALUES('go-chan','go','Q?'," +
      "'- unbuffered blocks\n- buffered fills\n- leaks')," +
      "('bug:uaf','bugs','Find it','- Bug class: heap-use-after-free\n-    int* p;')",
  );
  env = {
    DB: db as unknown as Env["DB"], AI: ai, DEV_USER: "me", DAILY_CAP: "50",
  } as unknown as Env;
});

const call = (path: string, init: RequestInit = {}) =>
  handle(
    new Request(`http://localhost${path}`, {
      ...init, headers: { "X-Drill": "1", "Content-Type": "application/json" },
    }),
    env,
  );
const post = (path: string, body: unknown) =>
  call(path, { method: "POST", body: JSON.stringify(body) });
const count = (table: string) =>
  (db.raw.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get() as { n: number }).n;

test("attempt through Workers AI is graded, stored and counted against the cap", async () => {
  const res = await post("/api/attempt", { card_id: "go-chan", answer: "my answer", secs: 9 });
  assert.equal(res.status, 200);
  const out = (await res.json()) as { grade: typeof GRADE; score: number };
  assert.deepEqual([out.grade.correct, out.grade.depth, out.grade.clarity], [4, 3, 5]);
  assert.equal(out.score, 4 * 0.5 + 3 * 0.3 + 5 * 0.2);
  assert.equal(count("attempts"), 1);
  assert.equal(count("usage"), 1);
  assert.match(JSON.stringify(calls[0].input.messages), /my answer/);
});

test("status tells the app how it will be graded", async () => {
  const grader = async () =>
    ((await (await call("/api/status")).json()) as { grader: string }).grader;
  assert.equal(await grader(), "workers-ai");
  env.LLM = "none";
  assert.equal(await grader(), "none");
  env.LLM = "anthropic";
  env.ANTHROPIC_API_KEY = "k";
  assert.equal(await grader(), "anthropic");
});

test("without a grader an attempt is refused before it costs anything", async () => {
  env.LLM = "none";
  const res = await post("/api/attempt", { card_id: "go-chan", answer: "x" });
  assert.equal(res.status, 409);
  assert.equal(count("usage"), 0);
  assert.equal(calls.length, 0);
});

test("a failing grader is a 502 and stores nothing", async () => {
  script = async () => {
    throw new Error("quota");
  };
  const res = await post("/api/attempt", { card_id: "go-chan", answer: "x" });
  assert.equal(res.status, 502);
  assert.equal(count("attempts"), 0);
});

test("reveal returns the reference and the checklist without grading", async () => {
  const res = await post("/api/reveal", { card_id: "go-chan" });
  assert.deepEqual(await res.json(), {
    ref: "- unbuffered blocks\n- buffered fills\n- leaks",
    points: ["unbuffered blocks", "buffered fills", "leaks"],
  });
  assert.equal(count("usage") + count("attempts") + calls.length, 0);
  assert.equal((await post("/api/reveal", { card_id: "nope" })).status, 404);
});

test("self-grade scores the ticked share, stores it and schedules the card", async () => {
  env.LLM = "none"; // works without any model
  const body = { card_id: "go-chan", hit: [0, 2], answer: "spoken", secs: 30 };
  const res = await post("/api/self-grade", body);
  assert.equal(res.status, 200);
  const out = (await res.json()) as {
    grade: { correct: number; missed: string[]; followup: string }; score: number;
    interval: number; ref: string;
  };
  assert.equal(out.score, 3.33);
  assert.deepEqual([out.grade.correct, out.grade.missed, out.grade.followup],
                   [3, ["buffered fills"], ""]);
  assert.equal(out.interval, 1); // q >= 3 on a new card: first interval is 1 day
  assert.equal(out.ref, "- unbuffered blocks\n- buffered fills\n- leaks");
  const row = db.raw.prepare("SELECT answer, score, correct, secs, detail FROM attempts").get() as
    Record<string, unknown>;
  assert.deepEqual(
    [row.answer, row.score, row.correct, row.secs, JSON.parse(String(row.detail))],
    ["spoken", 3.33, 3, 30, { mode: "self", points: 3, hit: [0, 2] }],
  );
  assert.equal(count("usage"), 0); // no quota: no model was used
  assert.equal(count("card_state"), 1);
});

test("self-grade extremes, exercise checklists and a failing score resets the card", async () => {
  const score = async (card: string, hit: number[]) => {
    const res = await post("/api/self-grade", { card_id: card, hit });
    return ((await res.json()) as { score: number }).score;
  };
  assert.equal(await score("go-chan", [0, 1, 2]), 5);
  assert.equal(await score("go-chan", []), 0);
  assert.equal(await score("bug:uaf", [0, 1, 2]), 5); // fixed 3-point list, not the diff lines
  assert.equal(await score("bug:uaf", [0]), 1.67);
  const state = db.raw.prepare("SELECT reps FROM card_state WHERE card_id = 'go-chan'").get();
  assert.deepEqual({ ...state }, { reps: 0 });
});

test("self-grade validates its input", async () => {
  const status = async (body: object) => (await post("/api/self-grade", body)).status;
  assert.equal(await status({ card_id: "go-chan", hit: [3] }), 400); // out of range
  assert.equal(await status({ card_id: "go-chan", hit: [-1] }), 400);
  assert.equal(await status({ card_id: "go-chan", hit: [0.5] }), 400);
  assert.equal(await status({ card_id: "go-chan", hit: "all" }), 400);
  assert.equal(await status({ card_id: "go-chan" }), 400);
  assert.equal(await status({ card_id: "nope", hit: [] }), 404);
  assert.equal(count("attempts"), 0);
  assert.equal(await status({ card_id: "go-chan", hit: [1, 1] }), 200); // duplicates count once
});
