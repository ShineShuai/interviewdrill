// Boot the real Worker in workerd (`wrangler dev`) on a throwaway local D1 and probe it over HTTP:
// what the node-based tests cannot show (real D1, real asset layer, real _headers processing).
// No Cloudflare login and no network. The AI binding only runs remotely, so it is left out and
// LLM is "none": grading goes through "Grade myself".
// Usage: node scripts/smoke.ts [PORT]      (or: npm run smoke; needs `npm install`)
import { execFileSync, spawn } from "node:child_process";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { request } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseCards, syncSql } from "../src/cards.ts";

const root = fileURLToPath(new URL("..", import.meta.url));
const port = Number(process.argv[2] ?? 8799);
const base = `http://127.0.0.1:${port}`;
const tmp = mkdtempSync(join(tmpdir(), "drill-smoke-"));
const config = join(root, "wrangler.smoke.toml"); // next to wrangler.toml: paths are relative
const state = join(tmp, "state");

const cardsDir = new URL("../../cards/", import.meta.url);
const cards = readdirSync(cardsDir)
  .filter((f) => f.endsWith(".md"))
  .sort()
  .flatMap((f) => parseCards(f.slice(0, -3), readFileSync(new URL(f, cardsDir), "utf8")));

const wrangler = (...args: string[]): string =>
  execFileSync("npx", ["wrangler", ...args], { cwd: root, encoding: "utf8", stdio: "pipe" });

interface Reply {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  text: string;
  json: () => any; // eslint-disable-line @typescript-eslint/no-explicit-any
}
/** node:http instead of fetch: the Host header has to be settable. */
const call = (method: string, path: string, headers: Record<string, string> = {}, body?: unknown) =>
  new Promise<Reply>((ok, fail) => {
    const req = request(`${base}${path}`, { method, headers }, (res) => {
      const parts: Buffer[] = [];
      res.on("data", (c) => parts.push(c)).on("end", () => {
        const text = Buffer.concat(parts).toString("utf8");
        ok({ status: res.statusCode ?? 0, headers: res.headers, text, json: () => JSON.parse(text) });
      });
    });
    req.on("error", fail);
    req.end(body === undefined ? undefined : JSON.stringify(body));
  });
const api = (method: string, path: string, body?: unknown) =>
  call(method, path, { "X-Drill": "1", "Content-Type": "application/json" }, body);

const results: [string, string | null][] = [];
const check = async (name: string, fn: () => Promise<void>): Promise<void> => {
  try {
    await fn();
    results.push([name, null]);
  } catch (e) {
    results.push([name, e instanceof Error ? e.message.split("\n")[0] : String(e)]);
  }
};

async function boot(): Promise<() => void> {
  const toml = readFileSync(join(root, "wrangler.toml"), "utf8")
    .replace(/\[ai\]\nbinding = "AI"\n\n?/, "")
    .replace('LLM = "workers-ai"', 'LLM = "none"');
  writeFileSync(config, toml);
  writeFileSync(join(tmp, "cards.sql"), syncSql(cards));
  for (const file of [join(root, "schema.sql"), join(tmp, "cards.sql")]) {
    wrangler("d1", "execute", "interviewdrill", "--local", "-c", config, "--persist-to", state, "--file", file);
  }
  const dev = spawn(
    "npx",
    ["wrangler", "dev", "-c", config, "--port", String(port), "--ip", "127.0.0.1",
      "--persist-to", state, "--var", "DEV_USER:me"],
    { cwd: root, detached: true, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, CI: "1" } },
  );
  const stop = () => {
    try {
      process.kill(-(dev.pid as number), "SIGTERM"); // the whole group: npx, node and workerd
    } catch {
      // already gone
    }
  };
  let log = "";
  await new Promise<void>((ok, fail) => {
    const timer = setTimeout(() => fail(new Error(`wrangler dev not ready in 120 s:\n${log}`)), 120_000);
    const feed = (c: Buffer) => {
      log += c.toString();
      if (log.includes("Ready on")) {
        clearTimeout(timer);
        ok();
      }
    };
    dev.stdout.on("data", feed);
    dev.stderr.on("data", feed);
    dev.on("exit", (code) => fail(new Error(`wrangler dev exited (${code}):\n${log}`)));
  });
  return stop;
}

function headerRules(): Record<string, string> {
  const lines = readFileSync(join(root, "public", "_headers"), "utf8").split("\n").slice(1);
  return Object.fromEntries(
    lines.filter((l) => l.includes(":")).map((l) => {
      const i = l.indexOf(":");
      return [l.slice(0, i).trim().toLowerCase(), l.slice(i + 1).trim()];
    }),
  );
}

let stop = () => {};
try {
  stop = await boot();
  const ids: string[] = [];

  await check("status: every card is due, nobody grades but you", async () => {
    const r = await api("GET", "/api/status");
    assert.equal(r.status, 200);
    const s = r.json();
    assert.equal(s.user, "me");
    assert.equal(s.due, cards.length);
    assert.equal(s.grader, "none");
    assert.equal(r.headers["cache-control"], "no-store");
  });
  await check("next: cards come back, limited by n", async () => {
    const r = await api("GET", "/api/next?n=3");
    assert.equal(r.json().cards.length, 3);
    ids.push(...r.json().cards.map((c: { id: string }) => c.id));
  });
  await check("reveal and self-grade: schedule moves, attempt counts", async () => {
    const rev = await api("POST", "/api/reveal", { card_id: ids[0] });
    assert.ok(rev.json().ref && rev.json().points.length > 0);
    const g = await api("POST", "/api/self-grade", { card_id: ids[0], hit: [0], answer: "x", secs: 3 });
    assert.equal(g.status, 200);
    assert.ok(g.json().interval > 0);
    assert.equal((await api("GET", "/api/status")).json().today.n, 1);
  });
  await check("attempt without a grader is refused (409)", async () => {
    assert.equal((await api("POST", "/api/attempt", { card_id: ids[1], answer: "x" })).status, 409);
  });
  await check("sync: push is idempotent, pull returns the events", async () => {
    const at = (n: number) => ({ uid: `smoke:${n}`, key: ids[1], ts: `2026-11-0${n}T09:00:00Z`,
      q: 3, passed: null, total: null, secs: 0 });
    const body = { attempts: [at(1), at(2)] };
    assert.deepEqual((await api("POST", "/api/sync/push", body)).json(),
      { accepted: 2, duplicates: 0, skipped: [] });
    assert.deepEqual((await api("POST", "/api/sync/push", body)).json(),
      { accepted: 0, duplicates: 2, skipped: [] });
    assert.equal((await api("GET", "/api/sync/pull?after=0")).json().attempts.length, 3);
  });
  await check("export and report see all of it", async () => {
    const ex = (await api("GET", "/api/export")).json();
    assert.equal(ex.attempts.length, 3);
    assert.deepEqual(ex.mock_sessions, []);
    assert.equal((await api("GET", "/api/report?days=14")).status, 200);
  });
  await check("mock: home works, turns need a model (409)", async () => {
    assert.equal((await api("GET", "/api/mock")).json().grader, "none");
    const r = await api("POST", "/api/mock/turn", { problem: "P", n: 3, turns: [] });
    assert.equal(r.status, 409);
  });
  await check("auth fails closed off localhost; POST needs X-Drill", async () => {
    const off = await call("GET", "/api/status", { Host: "drill.example.com" });
    assert.equal(off.status, 500);
    assert.match(off.text, /auth not configured/);
    assert.equal((await call("POST", "/api/self-grade", {}, {})).status, 403);
  });
  await check("assets carry the _headers rules", async () => {
    const want = headerRules();
    for (const path of ["/", "/app.js", "/manifest.webmanifest", "/py/worker.js"]) {
      const r = await call("GET", path);
      assert.equal(r.status, 200, path);
      for (const [name, value] of Object.entries(want)) assert.equal(r.headers[name], value, `${path} ${name}`);
    }
  });
  await check("assets have the right content types", async () => {
    const types: [string, string][] = [
      ["/manifest.webmanifest", "application/manifest+json"], ["/icon.svg", "image/svg+xml"],
    ];
    if (existsSync(join(root, "public/pyodide"))) types.push(["/pyodide/pyodide.asm.wasm", "application/wasm"]);
    if (existsSync(join(root, "public/icons"))) types.push(["/icons/apple-touch-icon.png", "image/png"]);
    for (const [path, type] of types) {
      assert.equal(String((await call("GET", path)).headers["content-type"]).split(";")[0], type, path);
    }
  });
} catch (e) {
  results.push(["boot", e instanceof Error ? e.message : String(e)]);
} finally {
  stop();
  rmSync(config, { force: true });
  rmSync(tmp, { recursive: true, force: true });
}

for (const [name, err] of results) console.log(`${err ? "FAIL" : "ok  "} ${name}${err ? `\n     ${err}` : ""}`);
const failed = results.filter(([, err]) => err).length;
console.log(failed ? `${failed} failed` : `all ${results.length} passed`);
process.exit(failed ? 1 : 0);
