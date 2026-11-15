// Copy the Pyodide runtime from node_modules into public/pyodide (served as static assets).
// Usage: node scripts/vendor-pyodide.mjs
import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export const FILES = [
  "pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json",
];

/** Copies FILES from src to dest; throws with a hint if the package is missing or incomplete. */
export function vendor(src, dest, files = FILES) {
  if (!existsSync(src)) throw new Error(`${src} not found: run \`npm install\` first`);
  const missing = files.filter((f) => !existsSync(join(src, f)));
  if (missing.length) {
    const found = readdirSync(src).join(", ");
    throw new Error(`missing in ${src}: ${missing.join(", ")}; found: ${found}`);
  }
  mkdirSync(dest, { recursive: true });
  for (const f of files) copyFileSync(join(src, f), join(dest, f));
  return files;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const root = fileURLToPath(new URL("..", import.meta.url));
  try {
    const src = join(root, "node_modules", "pyodide");
    vendor(src, join(root, "public", "pyodide"));
    const { version } = JSON.parse(readFileSync(join(src, "package.json"), "utf8"));
    console.log(`vendored Pyodide ${version} into public/pyodide`);
  } catch (e) {
    console.error(String(e.message));
    process.exit(1);
  }
}
