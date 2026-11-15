// Pure logic for running kata tests through a Worker-like adapter (browser worker or test fake).
// A worker is { run(code, input) -> Promise<{rc, out, err}>, kill() }; spawn() -> Promise<worker>.

export const BROWSER_FACTOR = 5; // Python in WebAssembly is several times slower than CPython
export const GROUPS = ["ex", "small", "large"];

/** Same normalization as norm_out() in kata.py. */
export const norm = (s) =>
  s.trim().split(/\r?\n/).map((ln) => ln.trimEnd()).join("\n");

const groupOf = (name) => /^[a-z]+/.exec(name)?.[0] ?? "";

/** Resolves with the promise's value, or null after `ms`. */
function withTimeout(promise, ms) {
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve(null), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

export function verdictOf(res, expected) {
  if (res === null) return "TLE";
  if (res.rc !== 0) return "RE";
  return norm(res.out) === norm(expected) ? "OK" : "WA";
}

/**
 * Run every test with a per-test time limit of timeLimitS * factor. A test that exceeds it is
 * a TLE: its worker is killed (the program may be in an endless loop) and a fresh one is spawned.
 */
export async function runTests({
  code, tests, timeLimitS, spawn, factor = BROWSER_FACTOR, onProgress = () => {},
}) {
  const limitMs = timeLimitS * 1000 * factor;
  let worker = await spawn();
  const results = [];
  try {
    for (const [i, t] of tests.entries()) {
      onProgress(i + 1, tests.length);
      const t0 = performance.now();
      const res = await withTimeout(worker.run(code, t.input), limitMs);
      const secs = (performance.now() - t0) / 1000;
      if (res === null) {
        worker.kill();
        worker = await spawn();
      }
      results.push({
        name: t.name, group: groupOf(t.name), verdict: verdictOf(res, t.expected), secs,
        input: t.input, expected: t.expected, got: res?.out ?? "", err: res?.err ?? "",
      });
    }
  } finally {
    worker.kill();
  }
  return results;
}

/** Counts per group and the first failure. Hidden tests show details only when revealed. */
export function summarize(results, reveal = false) {
  const groups = GROUPS.map((group) => {
    const rs = results.filter((r) => r.group === group);
    return { group, ok: rs.filter((r) => r.verdict === "OK").length, n: rs.length };
  }).filter((g) => g.n > 0);
  const bad = results.find((r) => r.verdict !== "OK");
  const showDetail = bad && (reveal || bad.group === "ex");
  return {
    passed: results.filter((r) => r.verdict === "OK").length,
    total: results.length,
    groups,
    failure: bad
      ? {
          name: bad.name, group: bad.group, verdict: bad.verdict, secs: bad.secs,
          err: bad.verdict === "RE" ? bad.err.slice(-300) : "",
          detail: showDetail
            ? { input: bad.input.slice(0, 500), expected: bad.expected.slice(0, 200),
                got: bad.got.slice(0, 200) }
            : null,
        }
      : null,
  };
}
