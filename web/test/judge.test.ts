import assert from "node:assert/strict";
import { test } from "node:test";
import { parseGrade, score, systemFor } from "../src/judge.ts";

test("exercise kinds get a grading hint, plain cards do not", () => {
  const plain = systemFor("go-chan");
  assert.ok(systemFor("bug:uaf").startsWith(plain));
  assert.match(systemFor("bug:uaf"), /debugging exercise/);
  assert.match(systemFor("problem:max-sum"), /coding exercise/);
  assert.doesNotMatch(plain, /exercise/);
  assert.equal(systemFor("bugfix"), plain); // prefix needs the colon
});

test("parseGrade clamps and truncates untrusted model output", () => {
  const g = parseGrade({
    correct: 9, depth: -2, clarity: "3.6", missed: ["a", 5, null], followup: "x".repeat(900),
    errors: [{ quote: "q", why: "w" }, null, { quote: 1 }],
  });
  assert.deepEqual([g.correct, g.depth, g.clarity], [5, 0, 4]);
  assert.deepEqual(g.missed, ["a", "5", ""]);
  assert.equal(g.followup.length, 500);
  assert.deepEqual(g.errors, [
    { quote: "q", why: "w" }, { quote: "", why: "" }, { quote: "1", why: "" },
  ]);
  assert.equal(score({ ...g, correct: 5, depth: 5, clarity: 5 }), 5);
});
