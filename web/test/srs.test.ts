import assert from "node:assert/strict";
import { test } from "node:test";
import { iso, schedule, streak } from "../src/srs.ts";

test("failing answer resets interval and lowers ease", () => {
  const s = schedule({ ease: 2.5, interval: 10, reps: 4 }, 2);
  assert.deepEqual([s.interval, s.reps], [1, 0]);
  assert.ok(s.ease < 2.5 && s.ease >= 1.3);
});

test("intervals progress 1d, 3d, then grow by ease", () => {
  let s = schedule({ ease: 2.5, interval: 0, reps: 0 }, 5);
  assert.deepEqual([s.interval, s.reps], [1, 1]);
  s = schedule(s, 5);
  assert.deepEqual([s.interval, s.reps], [3, 2]);
  assert.ok(schedule(s, 5).interval > 3);
});

test("ease never drops below 1.3", () => {
  let s = { ease: 1.35, interval: 5, reps: 3 };
  for (let i = 0; i < 5; i++) s = schedule(s, 3);
  assert.ok(s.ease >= 1.3);
});

test("streak", () => {
  assert.equal(streak([], "2026-11-14"), 0);
  assert.equal(streak(["2026-11-14", "2026-11-13", "2026-11-12"], "2026-11-14"), 3);
  assert.equal(streak(["2026-11-13", "2026-11-12"], "2026-11-14"), 2); // today not yet done
  assert.equal(streak(["2026-11-14", "2026-11-12"], "2026-11-14"), 1); // gap
  assert.equal(streak(["2026-11-11"], "2026-11-14"), 0); // lapsed
});

test("iso is second-precision UTC", () => {
  assert.equal(iso(new Date("2026-11-14T10:20:30.999Z")), "2026-11-14T10:20:30Z");
});
