// Boot the real Worker in workerd (`wrangler dev`) on a throwaway local D1 seeded with the real
// cards: what the node-based tests cannot show (real D1, real asset layer, real _headers).
// Shared by scripts/smoke.ts (HTTP checks) and test_web_ui.py (browser, via workerd-server.ts).
// No Cloudflare login and no network. The AI binding only runs remotely, so it is left out and
// LLM is "none": grading goes through "Grade myself".
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseCards, syncSql } from "../src/cards.ts";

export const root = fileURLToPath(new URL("..", import.meta.url));

const cardsDir = new URL("../../cards/", import.meta.url);
export const cards = readdirSync(cardsDir)
  .filter((f) => f.endsWith(".md"))
  .sort()
  .flatMap((f) => parseCards(f.slice(0, -3), readFileSync(new URL(f, cardsDir), "utf8")));

const wrangler = (...args: string[]): string =>
  execFileSync("npx", ["wrangler", ...args], { cwd: root, encoding: "utf8", stdio: "pipe" });

/** Start the Worker on `port` (user "me"); resolves with a function that stops it and cleans up. */
export async function boot(port: number): Promise<() => void> {
  const tmp = mkdtempSync(join(tmpdir(), "interviewdrill-workerd-"));
  const config = join(root, `wrangler.smoke-${port}.toml`); // next to wrangler.toml: paths are relative
  const state = join(tmp, "state");
  let dev: ReturnType<typeof spawn> | undefined;
  const stop = (): void => {
    try {
      process.kill(-(dev?.pid as number), "SIGTERM"); // the whole group: npx, node and workerd
    } catch {
      // not started or already gone
    }
    rmSync(config, { force: true });
    rmSync(tmp, { recursive: true, force: true });
  };
  try {
    const toml = readFileSync(join(root, "wrangler.toml"), "utf8")
      .replace(/\[ai\]\nbinding = "AI"\n\n?/, "")
      .replace('LLM = "workers-ai"', 'LLM = "none"');
    writeFileSync(config, toml);
    writeFileSync(join(tmp, "cards.sql"), syncSql(cards));
    for (const file of [join(root, "schema.sql"), join(tmp, "cards.sql")]) {
      wrangler("d1", "execute", "interviewdrill", "--local", "-c", config, "--persist-to", state, "--file", file);
    }
    const child = spawn(
      "npx",
      ["wrangler", "dev", "-c", config, "--port", String(port), "--ip", "127.0.0.1",
        "--persist-to", state, "--var", "DEV_USER:me"],
      { cwd: root, detached: true, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, CI: "1" } },
    );
    dev = child;
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
      child.stdout?.on("data", feed);
      child.stderr?.on("data", feed);
      child.on("exit", (code) => fail(new Error(`wrangler dev exited (${code}):\n${log}`)));
    });
  } catch (e) {
    stop();
    throw e;
  }
  return stop;
}
