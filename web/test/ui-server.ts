// Local server for browser tests (test_web_ui.py): public/ served with the real _headers,
// /api/* handled by the real Worker on an in-memory D1 seeded with the real cards, Workers AI
// replaced by a stub. Pyodide is replaced by a stand-in that runs the program with CPython,
// unless UI_PYODIDE=real: then the runtime vendored into public/pyodide (`npm run vendor`)
// is served and runs under the real CSP.
// Usage: node test/ui-server.ts PORT   (env UI_LLM=none|workers-ai, UI_AI_FAIL=1, UI_PYODIDE=real)
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { extname, normalize, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Env } from "../src/env.ts";
import { parseCards, syncSql } from "../src/cards.ts";
import { handle } from "../src/index.ts";
import { fakeD1 } from "./fake-d1.ts";

const here = (rel: string): string => fileURLToPath(new URL(rel, import.meta.url));
const PUBLIC = here("../public");
const CARDS = here("../../cards");
const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
  ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json", ".py": "text/plain",
  ".webmanifest": "application/manifest+json", ".wasm": "application/wasm",
  ".zip": "application/zip",
};
const REAL_PYODIDE = process.env.UI_PYODIDE === "real";

/** Headers of the `/*` rule in public/_headers (the same file Cloudflare applies). */
function sharedHeaders(): Record<string, string> {
  const out: Record<string, string> = {};
  let inRule = false;
  for (const ln of readFileSync(`${PUBLIC}/_headers`, "utf8").split("\n")) {
    if (!ln.startsWith(" ") && ln.trim()) inRule = ln.trim() === "/*";
    else if (inRule && ln.includes(":")) {
      const i = ln.indexOf(":");
      out[ln.slice(0, i).trim()] = ln.slice(i + 1).trim();
    }
  }
  return out;
}

const PROBLEM = {
  id: "problem:sum2",
  topic: "problems",
  question:
    "Problem: Sum two numbers - explain how you would solve it.\n\nRead a and b, print a+b.",
  ref: "- Intended approach: add them",
};
const SPEC = {
  id: "sum2", time_limit: 1,
  tests: [
    { name: "ex1", input: "1 2\n", expected: "3\n" },
    { name: "small01", input: "5 5\n", expected: "10\n" },
  ],
};
const GRADE = { correct: 4, depth: 3, clarity: 5, missed: ["the missed point"],
  errors: [{ quote: "my claim", why: "it is wrong" }], followup: "Why is that?" };

// Stand-in for pyodide.js, loaded by the real worker.js: run_case goes to CPython via sync XHR.
const FAKE_PYODIDE = `
const XHR = XMLHttpRequest; // kept: worker.js blocks the global after init
self.loadPyodide = async () => ({
  runPython() {},
  globals: { get: () => (code, input) => {
    const x = new XHR();
    x.open("POST", "/__py", false);
    x.send(JSON.stringify({ code, input }));
    return x.responseText;
  } },
});
`;
const DRIVER = `
import json, sys
ns = {}
exec(compile(open(sys.argv[1]).read(), "harness.py", "exec"), ns)
d = json.load(sys.stdin)
print(ns["run_case"](d["code"], d["input"]))
`;

// Probe run by the page (/__csp.html) and by a Worker: what the real _headers CSP allows.
const PROBE = `
const out = {};
const violated = [];
self.addEventListener("securitypolicyviolation", (e) => violated.push(e.violatedDirective));
const note = (k, f) => { try { f(); out[k] = "ran"; } catch (e) { out[k] = e.name; } };
note("eval", () => eval("1"));
note("newFunction", () => new Function("return 1"));
(async () => {
  try {
    await WebAssembly.compile(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0]));
    out.wasm = "ok";
  } catch (e) { out.wasm = e.name; }
  const other = location.protocol + "//localhost:" + location.port + "/";
  try { await fetch(other, { mode: "no-cors" }); out.fetchOtherOrigin = "ran"; }
  catch { out.fetchOtherOrigin = "blocked"; }
  out.violated = violated;
  if (self.document) self.__csp = out; else postMessage(out);
})();
`;

const db = fakeD1();
const cards = readdirSync(CARDS).filter((f) => f.endsWith(".md"))
  .flatMap((f) => parseCards(f.slice(0, -3), readFileSync(`${CARDS}/${f}`, "utf8")));
db.raw.exec(syncSql([...cards, PROBLEM]));

const env = {
  DB: db,
  AI: {
    run: async (_model: string, input: Record<string, unknown>) => {
      if (process.env.UI_AI_FAIL) throw new Error("ai down");
      return "audio" in input ? { text: "spoken words" } : { response: JSON.stringify(GRADE) };
    },
  },
  LLM: process.env.UI_LLM ?? "workers-ai",
  DEV_USER: "me",
  DAILY_CAP: "100",
} as unknown as Env;

const read = (req: import("node:http").IncomingMessage): Promise<Buffer> =>
  new Promise((ok) => {
    const parts: Buffer[] = [];
    req.on("data", (c) => parts.push(c)).on("end", () => ok(Buffer.concat(parts)));
  });

createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  const send = (status: number, body: string | Uint8Array, type: string): void => {
    res.writeHead(status, { ...sharedHeaders(), "Content-Type": type });
    res.end(body);
  };
  if (url.pathname.startsWith("/api/")) {
    const body = req.method === "GET" ? undefined : await read(req);
    const r = await handle(new Request(`http://localhost${req.url}`, {
      method: req.method, headers: req.headers as Record<string, string>,
      body: body && new Uint8Array(body),
    }), env);
    res.writeHead(r.status, { ...sharedHeaders(), "Content-Type": "application/json" });
    res.end(Buffer.from(await r.arrayBuffer()));
  } else if (url.pathname === "/__py") {
    const out = execFileSync("python3", ["-c", DRIVER, `${PUBLIC}/py/harness.py`],
      { input: await read(req) });
    send(200, out.toString().trim(), "application/json");
  } else if (url.pathname === "/pyodide/pyodide.js" && !REAL_PYODIDE) {
    send(200, FAKE_PYODIDE, MIME[".js"]);
  } else if (url.pathname === "/__csp.html") {
    send(200, '<!doctype html><script src="/__csp.js"></script>', MIME[".html"]);
  } else if (url.pathname === "/__csp.js") {
    send(200, PROBE, MIME[".js"]);
  } else if (url.pathname === "/problems/sum2.json") {
    send(200, JSON.stringify(SPEC), MIME[".json"]);
  } else {
    const rel = normalize(url.pathname === "/" ? "/index.html" : url.pathname);
    try {
      send(200, readFileSync(join(PUBLIC, rel)), MIME[extname(rel)] ?? "application/octet-stream");
    } catch {
      send(404, "not found", "text/plain");
    }
  }
}).listen(Number(process.argv[2]), "127.0.0.1", () => console.log("ready"));
