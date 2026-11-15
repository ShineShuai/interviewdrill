import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { test } from "node:test";
import { checkCards, parseCards, syncSql } from "../src/cards.ts";

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

const card = (id: string, extra: object = {}) => ({
  id, topic: "t", question: "Q?", ref: "- r", ...extra,
});

test("checkCards rejects empty fields and duplicate ids", () => {
  assert.doesNotThrow(() => checkCards([card("a"), card("bug:b")]));
  assert.throws(() => checkCards([card("a", { ref: "" })]), /incomplete card: "a"/);
  assert.throws(() => checkCards([card("a"), card("a")]), /duplicate card id: a/);
});

test("syncSql upserts every card, quotes safely and prunes the rest", () => {
  const quoted = card("a", { question: "It's a 'test'\nwith lines" });
  const sql = syncSql([quoted, card("bug:b")]);
  const lines = sql.split("\n");
  assert.ok(sql.includes("VALUES('a', 't', 'It''s a ''test''\nwith lines', '- r')"));
  assert.ok(sql.includes("VALUES('bug:b', 't', 'Q?', '- r')"));
  assert.ok(sql.includes("DELETE FROM cards WHERE id NOT IN ('a', 'bug:b');"));
  const pruneState = "DELETE FROM card_state WHERE card_id NOT IN (SELECT id FROM cards);";
  assert.equal(lines.at(-1), pruneState);
  assert.equal(syncSql([]).split("\n").length, 2); // empty set still prunes everything
});
