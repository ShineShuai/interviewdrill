export interface CardState {
  ease: number;
  interval: number; // days
  reps: number;
}

/** SM-2 variant; q in 0..5. Mirrors schedule() in drill.py. */
export function schedule({ ease, interval, reps }: CardState, q: number): CardState {
  if (q < 3) return { ease: Math.max(1.3, ease - 0.2), interval: 1, reps: 0 };
  const miss = 5 - q;
  const e = Math.max(1.3, ease + 0.1 - miss * (0.08 + miss * 0.02));
  return { ease: e, interval: reps === 0 ? 1 : reps === 1 ? 3 : interval * e, reps: reps + 1 };
}

/** ISO-8601 UTC, second precision; sorts lexicographically. */
export const iso = (d: Date): string => d.toISOString().slice(0, 19) + "Z";

/** Consecutive practiced UTC days ending today (or yesterday). `days` is sorted descending. */
export function streak(days: string[], today: string): number {
  const num = (d: string): number => Date.parse(`${d}T00:00:00Z`) / 86_400_000;
  let expect = num(today);
  if (days.length > 0 && num(days[0]) === expect - 1) expect -= 1;
  let n = 0;
  for (const d of days) {
    if (num(d) !== expect) break;
    n++;
    expect--;
  }
  return n;
}

const DAY_MS = 86_400_000;

/**
 * Rebuild a card's schedule from its attempts, oldest first: the same fold as live scheduling,
 * so every device that has the same attempts ends in the same state. Null if there are none.
 */
export function replay(
  attempts: { ts: string; q: number }[],
): (CardState & { due: string }) | null {
  if (attempts.length === 0) return null;
  let state: CardState = { ease: 2.5, interval: 0, reps: 0 };
  for (const a of attempts) state = schedule(state, a.q);
  const last = Date.parse(attempts[attempts.length - 1].ts);
  return { ...state, due: iso(new Date(last + state.interval * DAY_MS)) };
}
