import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { test } from "node:test";
import { parseCards } from "../src/cards.ts";

test("parses id | question headings and bodies", () => {
  const text = "# title\n## a | Why | pipe?\n- x\n- y\n\n## b | Q2\n- z\n";
  assert.deepEqual(parseCards("t", text), [
    { id: "a", topic: "t", question: "Why | pipe?", ref: "- x\n- y" },
    { id: "b", topic: "t", question: "Q2", ref: "- z" },
  ]);
});

test("provenance comments are not part of the reference", () => {
  const text = "## a | Q?\n<!-- src: go/spec#1; verified: quote+entailment -->\n- x\n";
  assert.deepEqual(parseCards("t", text), [{ id: "a", topic: "t", question: "Q?", ref: "- x" }]);
});

test("shared cards/ directory is well formed", () => {
  const dir = new URL("../../cards/", import.meta.url);
  const cards = readdirSync(dir)
    .filter((f) => f.endsWith(".md"))
    .flatMap((f) => parseCards(f.slice(0, -3), readFileSync(new URL(f, dir), "utf8")));
  assert.ok(cards.length >= 19);
  assert.equal(new Set(cards.map((c) => c.id)).size, cards.length);
  assert.ok(cards.every((c) => c.id && c.question && c.ref));
});
