// Upsert ../cards/*.md (shared with the CLI) into D1.
// Usage: node scripts/sync-cards.ts [--local|--remote]
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseCards } from "../src/cards.ts";

const target = process.argv[2] === "--local" ? "--local" : "--remote";
const dir = new URL("../../cards/", import.meta.url);
const cards = readdirSync(dir)
  .filter((f) => f.endsWith(".md"))
  .sort()
  .flatMap((f) => parseCards(f.slice(0, -3), readFileSync(new URL(f, dir), "utf8")));

const seen = new Set<string>();
for (const c of cards) {
  if (!c.id || !c.question || !c.ref) throw new Error(`incomplete card: ${JSON.stringify(c.id)}`);
  if (seen.has(c.id)) throw new Error(`duplicate card id: ${c.id}`);
  seen.add(c.id);
}

const q = (s: string): string => `'${s.replaceAll("'", "''")}'`;
const sql = [
  ...cards.map(
    (c) =>
      `INSERT INTO cards(id, topic, question, ref) VALUES(${q(c.id)}, ${q(c.topic)}, ` +
      `${q(c.question)}, ${q(c.ref)}) ON CONFLICT(id) DO UPDATE SET topic = excluded.topic, ` +
      "question = excluded.question, ref = excluded.ref;",
  ),
  `DELETE FROM cards WHERE id NOT IN (${cards.map((c) => q(c.id)).join(", ")});`,
  "DELETE FROM card_state WHERE card_id NOT IN (SELECT id FROM cards);",
].join("\n");

const file = join(tmpdir(), "drill-cards.sql");
writeFileSync(file, sql);
console.log(`syncing ${cards.length} cards (${target})`);
execFileSync("npx", ["wrangler", "d1", "execute", "drill", target, "--file", file], {
  stdio: "inherit",
});
