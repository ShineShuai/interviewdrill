// Runs the real public/py/worker.js and public/runner.js in a Node vm against the real harness.py.
// Only Pyodide itself is replaced: loadPyodide() returns a stand-in that executes the program
// with CPython (python3), so the message protocol, network blocking and harness are exercised.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { test } from "node:test";
import { runTests, summarize } from "../public/runner-core.js";

const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
const WORKER_JS = read("../public/py/worker.js");
const HARNESS = read("../public/py/harness.py");
const DRIVER = `
import json, sys
src, data = sys.argv[1], json.load(sys.stdin)
ns = {}
exec(compile(src, "harness.py", "exec"), ns)
print(ns["run_case"](data["code"], data["input"]))
`;

const cpython = (code, input) =>
  execFileSync("python3", ["-c", DRIVER, HARNESS], { input: JSON.stringify({ code, input }) })
    .toString().trim();

const workers = [];
let failInit = false;

class FakeWorker {
  constructor(url) {
    assert.equal(url, "/py/worker.js");
    const ctx = { performance, JSON, String, Error };
    ctx.self = ctx;
    ctx.XMLHttpRequest = class {};
    ctx.WebSocket = class {};
    ctx.importScripts = () => {};
    ctx.fetch = async (u) => {
      assert.equal(u, "/py/harness.py");
      return { text: async () => HARNESS };
    };
    ctx.loadPyodide = async (opts) => {
      assert.equal(opts.indexURL, "/pyodide/");
      if (failInit) throw new Error("wasm blocked by CSP");
      return { runPython: () => {}, globals: { get: (n) => (n === "run_case" ? cpython : null) } };
    };
    ctx.postMessage = (m) => queueMicrotask(() => this.onmessage?.({ data: m }));
    vm.createContext(ctx);
    vm.runInContext(WORKER_JS, ctx);
    this.ctx = ctx;
    this.terminated = false;
    workers.push(this);
  }
  postMessage(m) {
    queueMicrotask(() => this.ctx.onmessage({ data: m }));
  }
  terminate() {
    this.terminated = true;
  }
}
globalThis.Worker = FakeWorker;
const { spawnWorker } = await import("../public/runner.js");

const KADANE = `import sys
a = list(map(int, sys.stdin.read().split()))[1:]
best = cur = a[0]
for x in a[1:]:
    cur = max(x, cur + x)
    best = max(best, cur)
print(best)
`;
const TESTS = [
  { name: "ex1", input: "5\n1 -2 3 4 -1\n", expected: "7\n" },
  { name: "ex2", input: "3\n-3 -1 -2\n", expected: "-1\n" },
  { name: "small01", input: "1\n5\n", expected: "5\n" },
];

test("spawn resolves after init; run returns the harness result", async () => {
  const w = await spawnWorker();
  const r = await w.run("print(int(input()) * 2)", "21\n");
  assert.deepEqual([r.rc, r.out, r.err, r.type], [0, "42\n", "", "result"]);
  assert.ok(r.secs >= 0);
  w.kill();
  assert.ok(workers.at(-1).terminated);
});

test("the network is blocked before any user code can run", async () => {
  const w = await spawnWorker();
  const { ctx } = workers.at(-1);
  assert.throws(() => ctx.fetch("/api/export"), /network is disabled/);
  assert.throws(() => ctx.importScripts("/x.js"), /network is disabled/);
  assert.throws(() => new ctx.XMLHttpRequest());
  assert.throws(() => new ctx.WebSocket("wss://x"));
  w.kill();
});

test("the blocked network functions cannot be put back", async () => {
  // the prototype-chain escape (Object.getPrototypeOf(self).fetch) is covered in test_web_ui.py:
  // a vm global has no browser-like prototype chain
  const w = await spawnWorker();
  const { ctx } = workers.at(-1);
  assert.throws(() => Object.defineProperty(ctx, "fetch", { value: () => {} }), TypeError);
  assert.throws(() => ctx.fetch("/x"), /network is disabled/);
  w.kill();
});

test("a failing init rejects with the reason and terminates the worker", async () => {
  failInit = true;
  try {
    await assert.rejects(spawnWorker(), /wasm blocked by CSP/);
    assert.ok(workers.at(-1).terminated);
  } finally {
    failInit = false;
  }
});

test("end to end: correct, wrong and crashing solutions through runTests", async () => {
  const run = (code) => runTests({ code, tests: TESTS, timeLimitS: 2, spawn: spawnWorker });
  const good = await run(KADANE);
  assert.deepEqual(good.map((r) => r.verdict), ["OK", "OK", "OK"]);
  assert.equal(summarize(good).failure, null);

  const wrong = await run(KADANE.replace("best = cur = a[0]", "best = cur = 0"));
  assert.deepEqual(wrong.map((r) => r.verdict), ["OK", "WA", "WA"]); // also fails n = 1
  const s = summarize(wrong);
  assert.equal(s.failure.name, "ex2");
  assert.match(s.failure.detail.got, /^0/);

  const crash = await run("print(1 / 0)");
  assert.deepEqual(crash.map((r) => r.verdict), ["RE", "RE", "RE"]);
  assert.match(crash[0].err, /ZeroDivisionError/);
  assert.match(summarize(crash).failure.err, /ZeroDivisionError/);

  const syntax = await run("def f(:\n");
  assert.match(syntax[0].err, /SyntaxError/);
});

test("every run killed its worker", async () => {
  assert.ok(workers.length >= 5);
  assert.ok(workers.every((w) => w.terminated));
});
