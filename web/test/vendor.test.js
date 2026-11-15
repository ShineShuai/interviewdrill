import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { FILES, vendor } from "../scripts/vendor-pyodide.mjs";

const root = mkdtempSync(join(tmpdir(), "vendor-"));
after(() => rmSync(root, { recursive: true, force: true }));

const makePackage = (name, files) => {
  const dir = join(root, name);
  mkdirSync(dir);
  for (const f of files) writeFileSync(join(dir, f), `content of ${f}`);
  return dir;
};

test("copies the runtime files and creates the destination", () => {
  const src = makePackage("ok", [...FILES, "extra.txt"]);
  const dest = join(root, "out", "pyodide");
  assert.deepEqual(vendor(src, dest), FILES);
  assert.deepEqual(readdirSync(dest).sort(), [...FILES].sort()); // no extras copied
  const wasm = readFileSync(join(dest, "pyodide.asm.wasm"), "utf8");
  assert.equal(wasm, "content of pyodide.asm.wasm");
});

test("explains a missing package and an incomplete one", () => {
  assert.throws(() => vendor(join(root, "nope"), join(root, "x")), /run `npm install` first/);
  const src = makePackage("partial", FILES.slice(0, 2).concat(["other.js"]));
  const explains = (e) =>
    /missing in .*python_stdlib\.zip/.test(e.message) && /found: .*other\.js/.test(e.message);
  assert.throws(() => vendor(src, join(root, "y")), explains);
});
