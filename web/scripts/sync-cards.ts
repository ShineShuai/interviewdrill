// Upsert cards into D1: ../cards/*.md plus approved problems and bug exercises (export_cards.py).
// Usage: node scripts/sync-cards.ts [--local|--remote]   (PYTHON overrides python3)
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { checkCards, parseCards, syncSql, type Card } from "../src/cards.ts";

const target = process.argv[2] === "--local" ? "--local" : "--remote";
const dir = new URL("../../cards/", import.meta.url);
const markdown = readdirSync(dir)
  .filter((f) => f.endsWith(".md"))
  .sort()
  .flatMap((f) => parseCards(f.slice(0, -3), readFileSync(new URL(f, dir), "utf8")));

// Fail loudly: silently skipping exercises would make the prune step delete them from D1.
const exporter = fileURLToPath(new URL("../../export_cards.py", import.meta.url));
const exercises = JSON.parse(
  execFileSync(process.env.PYTHON ?? "python3", [exporter], {
    encoding: "utf8",
    maxBuffer: 64 << 20,
  }),
) as Card[];

const cards = [...markdown, ...exercises];
checkCards(cards);

const file = join(tmpdir(), "drill-cards.sql");
writeFileSync(file, syncSql(cards));
console.log(`syncing ${markdown.length} cards + ${exercises.length} exercises (${target})`);
execFileSync("npx", ["wrangler", "d1", "execute", "interviewdrill", target, "--file", file], {
  stdio: "inherit",
});
