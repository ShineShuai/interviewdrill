import assert from "node:assert/strict";
import { test } from "node:test";
import { norm, runTests, summarize, verdictOf } from "../public/runner-core.js";

const ok = (out) => ({ rc: 0, out, err: "" });
const never = new Promise(() => {});

/** A fake spawn(): each worker answers via `program(code, input)`; counts spawns and kills. */
function fake(program) {
  const stats = { spawned: 0, killed: 0 };
  const spawn = async () => {
    stats.spawned++;
    return { run: async (code, input) => program(code, input), kill: () => stats.killed++ };
  };
  return { spawn, stats };
}
const T = (name, input, expected) => ({ name, input, expected });

test("norm matches problems.py norm_out", () => {
  assert.equal(norm("  a  \r\nb\t\n\n"), "a\nb");
  assert.equal(norm("7\n"), "7");
  assert.equal(norm(""), "");
  assert.equal(norm("a\n \nb"), "a\n\nb");
});

test("verdictOf", () => {
  assert.equal(verdictOf(null, "1"), "TLE");
  assert.equal(verdictOf({ rc: 2, out: "1", err: "x" }, "1"), "RE");
  assert.equal(verdictOf(ok("1 \n"), "1"), "OK");
  assert.equal(verdictOf(ok("2"), "1"), "WA");
});

test("runTests classifies each test and always kills the worker", async () => {
  const { spawn, stats } = fake((code, input) =>
    input === "boom" ? { rc: 1, out: "", err: "Traceback" } : ok(input === "bad" ? "9" : "1"));
  const results = await runTests({
    code: "x", timeLimitS: 1, spawn,
    tests: [T("ex1", "a", "1"), T("small01", "bad", "1"), T("small02", "boom", "1")],
  });
  assert.deepEqual(results.map((r) => r.verdict), ["OK", "WA", "RE"]);
  assert.deepEqual(results.map((r) => r.group), ["ex", "small", "small"]);
  assert.deepEqual(stats, { spawned: 1, killed: 1 });
  assert.equal(results[1].got, "9");
});

test("exceeding the limit is a TLE; the worker is replaced and later tests run", async () => {
  const { spawn, stats } = fake((code, input) => (input === "loop" ? never : ok("1")));
  const progress = [];
  const results = await runTests({
    code: "x", timeLimitS: 0.01, factor: 1, spawn, onProgress: (i, n) => progress.push([i, n]),
    tests: [T("ex1", "a", "1"), T("large1", "loop", "1"), T("large2", "b", "1")],
  });
  assert.deepEqual(results.map((r) => r.verdict), ["OK", "TLE", "OK"]);
  assert.deepEqual(stats, { spawned: 2, killed: 2 }); // the stuck one and the final one
  assert.deepEqual(progress, [[1, 3], [2, 3], [3, 3]]);
  assert.ok(results[1].secs >= 0.009);
});

test("the time limit is scaled by the browser factor", async () => {
  const slow = fake(() => new Promise((res) => setTimeout(() => res(ok("1")), 40)));
  const tight = await runTests({ code: "x", timeLimitS: 0.01, factor: 1, spawn: slow.spawn,
                                 tests: [T("ex1", "a", "1")] });
  assert.equal(tight[0].verdict, "TLE");
  const roomy = await runTests({ code: "x", timeLimitS: 0.01, factor: 20, spawn: slow.spawn,
                                 tests: [T("ex1", "a", "1")] });
  assert.equal(roomy[0].verdict, "OK");
});

test("the worker is killed even when spawn or run throws", async () => {
  const stats = { killed: 0 };
  const spawn = async () => ({ run: async () => { throw new Error("worker died"); },
                               kill: () => stats.killed++ });
  await assert.rejects(runTests({ code: "x", timeLimitS: 1, spawn, tests: [T("ex1", "a", "1")] }),
                       /worker died/);
  assert.equal(stats.killed, 1);
  await assert.rejects(runTests({ code: "x", timeLimitS: 1, tests: [],
                                  spawn: async () => { throw new Error("no runtime"); } }),
                       /no runtime/);
});

const sample = [
  { name: "ex1", group: "ex", verdict: "OK" },
  { name: "ex2", group: "ex", verdict: "OK" },
  { name: "small01", group: "small", verdict: "OK" },
  { name: "small02", group: "small", verdict: "WA", secs: 0.1, input: "i".repeat(900),
    expected: "e", got: "g", err: "" },
  { name: "large1", group: "large", verdict: "TLE", secs: 10, input: "", expected: "", got: "",
    err: "" },
];

test("summarize hides details of hidden tests unless revealed", () => {
  const s = summarize(sample);
  assert.deepEqual([s.passed, s.total], [3, 5]);
  assert.deepEqual(s.groups, [
    { group: "ex", ok: 2, n: 2 }, { group: "small", ok: 1, n: 2 }, { group: "large", ok: 0, n: 1 },
  ]);
  assert.equal(s.failure.name, "small02");
  assert.equal(s.failure.detail, null);
  const r = summarize(sample, true);
  assert.equal(r.failure.detail.input.length, 500);
  assert.deepEqual([r.failure.detail.expected, r.failure.detail.got], ["e", "g"]);
});

test("summarize always shows failing public examples, and the error of a crash", () => {
  const crash = [{ name: "ex1", group: "ex", verdict: "RE", secs: 0, input: "1", expected: "2",
                   got: "", err: "x".repeat(400) + "ZeroDivisionError" }];
  const f = summarize(crash).failure;
  assert.equal(f.detail.input, "1");
  assert.equal(f.err.length, 300);
  assert.ok(f.err.endsWith("ZeroDivisionError"));
  assert.equal(summarize([{ name: "ex1", group: "ex", verdict: "OK" }]).failure, null);
  assert.deepEqual(summarize([]), { passed: 0, total: 0, groups: [], failure: null });
});
