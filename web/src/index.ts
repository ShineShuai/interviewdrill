import { authenticate } from "./auth.ts";
import type { Env } from "./env.ts";
import { HttpError, json } from "./http.ts";
import { checklist } from "./checklist.ts";
import { judge, score, type Grade } from "./judge.ts";
import { providerOf } from "./llm.ts";
import { iso, replay, schedule, streak, type CardState } from "./srs.ts";

interface Ctx {
  req: Request;
  env: Env;
  url: URL;
  user: string;
}
type Handler = (c: Ctx) => Promise<Response>;

const DAY_MS = 86_400_000;
const MAX_ANSWER = 4000;
const MAX_AUDIO = 8_000_000;
const NEW_STATE: CardState = { ease: 2.5, interval: 0, reps: 0 };

function clampInt(v: string | null, lo: number, hi: number, dflt: number): number {
  const n = Math.trunc(Number(v));
  return v !== null && Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : dflt;
}

async function readJson(req: Request): Promise<Record<string, unknown>> {
  try {
    const body = await req.json();
    if (body && typeof body === "object") return body as Record<string, unknown>;
  } catch {
    // fall through
  }
  throw new HttpError(400, "invalid JSON body");
}

/** Count one billable call (LLM or STT) per user per UTC day; throws 429 over the cap. */
async function takeQuota(env: Env, user: string): Promise<void> {
  const row = await env.DB.prepare(
    "INSERT INTO usage(user_id, day, calls) VALUES(?1, ?2, 1) " +
      "ON CONFLICT(user_id, day) DO UPDATE SET calls = calls + 1 RETURNING calls",
  )
    .bind(user, iso(new Date()).slice(0, 10))
    .first<{ calls: number }>();
  if ((row?.calls ?? 0) > Number(env.DAILY_CAP ?? 200)) {
    throw new HttpError(429, "daily cap reached");
  }
}

function toBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

interface Outcome {
  secs: number;
  answer: string;
  correct: number | null;
  depth: number | null;
  clarity: number | null;
  detail: unknown;
}

/** Reschedule the card from quality q and store its state and the attempt in one batch. */
async function recordAttempt(
  env: Env, user: string, cardId: string, q: number, o: Outcome,
): Promise<CardState> {
  const state =
    (await env.DB.prepare(
      "SELECT ease, interval, reps FROM card_state WHERE user_id = ?1 AND card_id = ?2",
    )
      .bind(user, cardId)
      .first<CardState>()) ?? NEW_STATE;
  const nx = schedule(state, q);
  const now = new Date();
  const due = iso(new Date(+now + nx.interval * DAY_MS));
  await env.DB.batch([
    env.DB.prepare(
      "INSERT INTO card_state(user_id, card_id, ease, interval, reps, due) " +
        "VALUES(?1, ?2, ?3, ?4, ?5, ?6) ON CONFLICT(user_id, card_id) " +
        "DO UPDATE SET ease = ?3, interval = ?4, reps = ?5, due = ?6",
    ).bind(user, cardId, nx.ease, nx.interval, nx.reps, due),
    env.DB.prepare(
      "INSERT INTO attempts(user_id, card_id, ts, secs, answer, correct, depth, clarity, " +
        "score, detail) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
    ).bind(
      user, cardId, iso(now), o.secs, o.answer, o.correct, o.depth, o.clarity, q,
      JSON.stringify(o.detail),
    ),
  ]);
  return nx;
}

const status: Handler = async ({ env, user }) => {
  const now = iso(new Date());
  const today = now.slice(0, 10);
  const [topics, done, days] = await Promise.all([
    env.DB.prepare(
      "SELECT c.topic AS topic, COUNT(*) AS due FROM cards c " +
        "LEFT JOIN card_state s ON s.card_id = c.id AND s.user_id = ?1 " +
        "WHERE COALESCE(s.due, '') <= ?2 GROUP BY c.topic ORDER BY c.topic",
    )
      .bind(user, now)
      .all<{ topic: string; due: number }>(),
    env.DB.prepare("SELECT COUNT(*) AS n FROM attempts WHERE user_id = ?1 AND ts >= ?2")
      .bind(user, today)
      .first<{ n: number }>(),
    env.DB.prepare(
      "SELECT DISTINCT substr(ts, 1, 10) AS d FROM attempts WHERE user_id = ?1 " +
        "ORDER BY d DESC LIMIT 400",
    )
      .bind(user)
      .all<{ d: string }>(),
  ]);
  return json({
    user,
    topics: topics.results,
    due: topics.results.reduce((sum, t) => sum + t.due, 0),
    today: { n: done?.n ?? 0 },
    streak: streak(days.results.map((r) => r.d), today),
    grader: providerOf(env), // "none": the app asks you to grade yourself
  });
};

/**
 * Due cards with their references, for a device that may lose its connection: the PWA caches
 * them and lets you grade yourself offline. Problems need the browser runner, so they stay out.
 */
const offline: Handler = async ({ env, url, user }) => {
  const n = clampInt(url.searchParams.get("n"), 1, 50, 30);
  const rows = await env.DB.prepare(
    "SELECT c.id, c.topic, c.question, c.ref FROM cards c " +
      "LEFT JOIN card_state s ON s.card_id = c.id AND s.user_id = ?1 " +
      "WHERE COALESCE(s.due, '') <= ?2 AND c.id NOT LIKE 'problem:%' " +
      "ORDER BY COALESCE(s.due, ''), RANDOM() LIMIT ?3",
  )
    .bind(user, iso(new Date()), n)
    .all<CardRow & { topic: string }>();
  return json({ cards: rows.results.map((c) => ({ ...c, points: checklist(c.id, c.ref) })) });
};

const next: Handler = async ({ env, url, user }) => {
  const n = clampInt(url.searchParams.get("n"), 1, 20, 5);
  const topic = url.searchParams.get("topic") || null;
  const rows = await env.DB.prepare(
    "SELECT c.id, c.topic, c.question FROM cards c " +
      "LEFT JOIN card_state s ON s.card_id = c.id AND s.user_id = ?1 " +
      "WHERE COALESCE(s.due, '') <= ?2 AND (?3 IS NULL OR c.topic = ?3) " +
      "ORDER BY COALESCE(s.due, ''), RANDOM() LIMIT ?4",
  )
    .bind(user, iso(new Date()), topic, n)
    .all();
  return json({ cards: rows.results });
};

const attempt: Handler = async ({ req, env, user }) => {
  const body = await readJson(req);
  const cardId = String(body.card_id ?? "");
  const answer = String(body.answer ?? "").trim();
  const secs = Math.min(3600, Math.max(0, Number(body.secs) || 0));
  if (!answer) throw new HttpError(400, "empty answer");
  if (answer.length > MAX_ANSWER) throw new HttpError(413, "answer too long");
  const card = await env.DB.prepare("SELECT id, question, ref FROM cards WHERE id = ?1")
    .bind(cardId)
    .first<{ id: string; question: string; ref: string }>();
  if (!card) throw new HttpError(404, "unknown card");
  if (providerOf(env) === "none") throw new HttpError(409, "no grader configured: grade yourself");

  await takeQuota(env, user);
  const grade = await judge(env, card, answer);
  const q = score(grade);
  const nx = await recordAttempt(env, user, cardId, q, {
    secs, answer, correct: grade.correct, depth: grade.depth, clarity: grade.clarity,
    detail: grade,
  });
  return json({ grade, score: q, interval: nx.interval, ref: card.ref });
};

interface CardRow {
  id: string;
  question: string;
  ref: string;
}

async function loadCard(env: Env, id: string): Promise<CardRow> {
  const card = await env.DB.prepare("SELECT id, question, ref FROM cards WHERE id = ?1")
    .bind(id)
    .first<CardRow>();
  if (!card) throw new HttpError(404, "unknown card");
  return card;
}

/** Show the reference and the checklist (no model, no quota) so the user can grade themselves. */
const reveal: Handler = async ({ req, env }) => {
  const card = await loadCard(env, String((await readJson(req)).card_id ?? ""));
  return json({ ref: card.ref, points: checklist(card.id, card.ref) });
};

/** The user ticks which checklist points their answer covered; the score is the share. */
const selfGrade: Handler = async ({ req, env, user }) => {
  const body = await readJson(req);
  const card = await loadCard(env, String(body.card_id ?? ""));
  const points = checklist(card.id, card.ref);
  const hit = body.hit;
  const valid = (i: unknown): boolean =>
    typeof i === "number" && Number.isInteger(i) && i >= 0 && i < points.length;
  if (!Array.isArray(hit) || !hit.every(valid)) {
    throw new HttpError(400, "hit must be a list of checklist numbers");
  }
  const ticked = new Set<number>(hit as number[]);
  const q = Math.round((500 * ticked.size) / points.length) / 100;
  const level = Math.round(q);
  const grade: Grade = {
    correct: level, depth: level, clarity: level, errors: [], followup: "",
    missed: points.filter((_, i) => !ticked.has(i)),
  };
  const nx = await recordAttempt(env, user, card.id, q, {
    secs: Math.min(3600, Math.max(0, Number(body.secs) || 0)),
    answer: String(body.answer ?? "").slice(0, MAX_ANSWER),
    correct: level, depth: level, clarity: level,
    detail: { mode: "self", points: points.length, hit: [...ticked].sort() },
  });
  return json({ grade, score: q, interval: nx.interval, ref: card.ref });
};

/** A problem run by the in-browser Python runner: no LLM, the client reports passed/total. */
const problemResult: Handler = async ({ req, env, user }) => {
  const body = await readJson(req);
  const cardId = String(body.card_id ?? "");
  const passed = Math.trunc(Number(body.passed));
  const total = Math.trunc(Number(body.total));
  if (!cardId.startsWith("problem:")) throw new HttpError(400, "not a problem");
  if (!(total >= 1 && total <= 1000 && passed >= 0 && passed <= total)) {
    throw new HttpError(400, "invalid counts");
  }
  const found = await env.DB.prepare("SELECT 1 AS found FROM cards WHERE id = ?1")
    .bind(cardId)
    .first();
  if (!found) throw new HttpError(404, "unknown card");
  const q = passed === total ? 5 : passed >= 0.8 * total ? 3 : 1;
  const nx = await recordAttempt(env, user, cardId, q, {
    secs: Math.min(3600, Math.max(0, Number(body.secs) || 0)), answer: "(browser tests)",
    correct: Math.round((5 * passed) / total), depth: null, clarity: null,
    detail: { mode: "browser", passed, total },
  });
  return json({ score: q, passed, total, interval: nx.interval });
};

const stt: Handler = async ({ req, env, user }) => {
  const audio = await req.arrayBuffer();
  if (audio.byteLength === 0 || audio.byteLength > MAX_AUDIO) {
    throw new HttpError(413, "audio size out of range");
  }
  await takeQuota(env, user);
  try {
    const out = (await env.AI.run("@cf/openai/whisper-large-v3-turbo", {
      audio: toBase64(audio),
      language: env.STT_LANG ?? "en",
    })) as { text?: string };
    return json({ text: (out.text ?? "").trim() });
  } catch (e) {
    console.error("stt", e);
    throw new HttpError(502, "transcription failed");
  }
};

const report: Handler = async ({ env, url, user }) => {
  const days = clampInt(url.searchParams.get("days"), 1, 365, 14);
  const since = iso(new Date(Date.now() - days * DAY_MS));
  const [topics, weakest, trend] = await Promise.all([
    env.DB.prepare(
      "SELECT c.topic AS topic, COUNT(*) AS n, AVG(a.score) AS score, " +
        "AVG(a.correct) AS correct, AVG(a.depth) AS depth, AVG(a.clarity) AS clarity " +
        "FROM attempts a " +
        "JOIN cards c ON c.id = a.card_id WHERE a.user_id = ?1 AND a.ts >= ?2 " +
        "GROUP BY c.topic ORDER BY score",
    )
      .bind(user, since)
      .all(),
    env.DB.prepare(
      "SELECT a.card_id AS card_id, c.question AS question, AVG(a.score) AS score, " +
        "COUNT(*) AS n FROM attempts a JOIN cards c ON c.id = a.card_id " +
        "WHERE a.user_id = ?1 AND a.ts >= ?2 GROUP BY a.card_id ORDER BY score LIMIT 5",
    )
      .bind(user, since)
      .all(),
    env.DB.prepare(
      "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n, AVG(score) AS score FROM attempts " +
        "WHERE user_id = ?1 AND ts >= ?2 GROUP BY day ORDER BY day",
    )
      .bind(user, since)
      .all(),
  ]);
  return json({ days, topics: topics.results, weakest: weakest.results, trend: trend.results });
};

const exportAll: Handler = async ({ env, user }) => {
  const [state, attempts] = await Promise.all([
    env.DB.prepare("SELECT * FROM card_state WHERE user_id = ?1").bind(user).all(),
    env.DB.prepare("SELECT * FROM attempts WHERE user_id = ?1 ORDER BY ts").bind(user).all(),
  ]);
  return json(
    { exported: iso(new Date()), user, card_state: state.results, attempts: attempts.results },
    200,
    { "Content-Disposition": 'attachment; filename="drill-export.json"' },
  );
};

// --- CLI sync: attempts are append-only events; a card's schedule is the replay of its events ---
const SYNC_KEY = /^[A-Za-z0-9:_.-]{1,80}$/; // any card id
const SYNC_TS = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/;
const MAX_SYNC = 200;

interface SyncAttempt {
  uid: string;
  key: string;
  ts: string;
  q: number;
  passed: number | null;
  total: number | null;
  secs: number;
  correct: number | null; // per-criterion grades of a graded card (0..5), if the sender has them
  depth: number | null;
  clarity: number | null;
  answer: string | null; // the typed answer, if the sender keeps it (the PWA's offline queue)
  mode: "cli" | "offline";
}

function parseSyncAttempt(raw: unknown, i: number): SyncAttempt {
  const bad = (why: string): never => {
    throw new HttpError(400, `attempts[${i}]: ${why}`);
  };
  if (!raw || typeof raw !== "object") return bad("not an object");
  const a = raw as Record<string, unknown>;
  if (typeof a.uid !== "string" || a.uid.length < 1 || a.uid.length > 80) bad("bad uid");
  if (typeof a.key !== "string" || !SYNC_KEY.test(a.key)) bad("key must be a card id");
  if (typeof a.ts !== "string" || !SYNC_TS.test(a.ts)) bad("ts must be UTC ...Z, seconds only");
  if (typeof a.q !== "number" || !(a.q >= 0 && a.q <= 5)) bad("q must be 0..5");
  const given = a.passed !== undefined && a.passed !== null;
  if (given && !(Number.isInteger(a.passed) && Number.isInteger(a.total))) bad("bad counts");
  const passed = given ? (a.passed as number) : null;
  const total = given ? (a.total as number) : null;
  if (passed !== null && total !== null && !(passed >= 0 && passed <= total && total <= 1000)) {
    bad("bad counts");
  }
  const level = (name: "correct" | "depth" | "clarity"): number | null => {
    const v = a[name];
    if (v === undefined || v === null) return null;
    if (!(typeof v === "number" && Number.isInteger(v) && v >= 0 && v <= 5)) {
      return bad(`${name} must be an integer 0..5`);
    }
    return v;
  };
  if (a.answer !== undefined && a.answer !== null && typeof a.answer !== "string") {
    bad("answer must be a string");
  }
  if (a.mode !== undefined && a.mode !== "cli" && a.mode !== "offline") bad("bad mode");
  return {
    uid: a.uid as string, key: a.key as string, ts: a.ts as string, q: a.q as number, passed,
    total, secs: Math.min(3600, Math.max(0, Number(a.secs) || 0)),
    correct: level("correct"), depth: level("depth"), clarity: level("clarity"),
    answer: typeof a.answer === "string" ? a.answer.slice(0, MAX_ANSWER) : null,
    mode: a.mode === "offline" ? "offline" : "cli",
  };
}

/** Rebuild card_state of these cards from their attempts. */
async function recompute(env: Env, user: string, cardIds: string[]): Promise<void> {
  for (const id of cardIds) {
    const rows = await env.DB.prepare(
      "SELECT ts, COALESCE(score, 0) AS q FROM attempts " +
        "WHERE user_id = ?1 AND card_id = ?2 ORDER BY ts, id",
    )
      .bind(user, id)
      .all<{ ts: string; q: number }>();
    const s = replay(rows.results);
    if (!s) continue;
    await env.DB.prepare(
      "INSERT INTO card_state(user_id, card_id, ease, interval, reps, due) " +
        "VALUES(?1, ?2, ?3, ?4, ?5, ?6) ON CONFLICT(user_id, card_id) " +
        "DO UPDATE SET ease = ?3, interval = ?4, reps = ?5, due = ?6",
    )
      .bind(user, id, s.ease, s.interval, s.reps, s.due)
      .run();
  }
}

const syncPush: Handler = async ({ req, env, user }) => {
  const body = await readJson(req);
  const list = body.attempts;
  if (!Array.isArray(list) || list.length > MAX_SYNC) {
    throw new HttpError(400, `attempts must be a list of at most ${MAX_SYNC}`);
  }
  const attempts = list.map(parseSyncAttempt);
  const cards = await env.DB.prepare("SELECT id FROM cards").all<{ id: string }>();
  const known = new Set(cards.results.map((c) => c.id));
  const skipped = new Set<string>();
  const accepted = attempts.filter((a) => known.has(a.key) || !skipped.add(a.key));
  const results = await env.DB.batch(
    accepted.map((a) =>
      env.DB.prepare(
        "INSERT OR IGNORE INTO attempts(user_id, card_id, ts, secs, answer, correct, depth, " +
          "clarity, score, detail, uid) " +
          "VALUES(?1, ?2, ?3, ?4, ?11, ?5, ?6, ?7, ?8, ?9, ?10)",
      ).bind(
        user, a.key, a.ts, a.secs,
        a.correct ??
          (a.passed !== null && a.total ? Math.round((5 * a.passed) / a.total) : Math.round(a.q)),
        a.depth, a.clarity, a.q,
        JSON.stringify({ mode: a.mode, passed: a.passed, total: a.total }), a.uid,
        a.answer ?? "(cli)",
      ),
    ),
  );
  const inserted = accepted.filter((_, i) => (results[i].meta?.changes ?? 0) > 0);
  await recompute(env, user, [...new Set(inserted.map((a) => a.key))]);
  return json({
    accepted: inserted.length, duplicates: accepted.length - inserted.length,
    skipped: [...skipped],
  });
};

interface PulledRow {
  id: number;
  uid: string;
  key: string;
  ts: string;
  q: number;
  secs: number | null;
  correct: number | null;
  depth: number | null;
  clarity: number | null;
  detail: string | null;
}

const syncPull: Handler = async ({ env, url, user }) => {
  const after = clampInt(url.searchParams.get("after"), 0, Number.MAX_SAFE_INTEGER, 0);
  const limit = clampInt(url.searchParams.get("limit"), 1, 500, 500);
  const rows = await env.DB.prepare(
    "SELECT id, COALESCE(uid, 'web:' || id) AS uid, card_id AS key, ts, " +
      "COALESCE(score, 0) AS q, secs, correct, depth, clarity, detail FROM attempts " +
      "WHERE user_id = ?1 AND id > ?2 ORDER BY id LIMIT ?3",
  )
    .bind(user, after, limit)
    .all<PulledRow>();
  const counts = (detail: string | null): { passed: number | null; total: number | null } => {
    try {
      const d = JSON.parse(detail ?? "null");
      return Number.isInteger(d?.passed) && Number.isInteger(d?.total)
        ? { passed: d.passed, total: d.total }
        : { passed: null, total: null };
    } catch {
      return { passed: null, total: null };
    }
  };
  const attempts = rows.results.map(({ detail, ...r }) => ({ ...r, ...counts(detail) }));
  return json({ attempts, next: attempts.length ? attempts[attempts.length - 1].id : after });
};

const ROUTES: Record<string, Handler> = {
  "GET /api/status": status,
  "GET /api/next": next,
  "GET /api/offline": offline,
  "POST /api/attempt": attempt,
  "POST /api/reveal": reveal,
  "POST /api/self-grade": selfGrade,
  "POST /api/problem/result": problemResult,
  "POST /api/sync/push": syncPush,
  "GET /api/sync/pull": syncPull,
  "POST /api/stt": stt,
  "GET /api/report": report,
  "GET /api/export": exportAll,
};

export async function handle(req: Request, env: Env): Promise<Response> {
  try {
    const identity = await authenticate(req, env);
    if (!identity) return json({ error: "unauthorized" }, 401);
    const { user, machine } = identity;
    // Custom header blocks cross-site form posts riding the Access cookie (CSRF).
    if (req.method !== "GET" && req.headers.get("X-Drill") !== "1") {
      return json({ error: "forbidden" }, 403);
    }
    const url = new URL(req.url);
    // A service token (the CLI) may only sync; everything else needs a signed-in person.
    if (machine && !url.pathname.startsWith("/api/sync/")) {
      return json({ error: "forbidden" }, 403);
    }
    const route = ROUTES[`${req.method} ${url.pathname}`];
    if (!route) return json({ error: "not found" }, 404);
    return await route({ req, env, url, user });
  } catch (e) {
    if (e instanceof HttpError) return json({ error: e.message }, e.status);
    console.error(e);
    return json({ error: "internal error" }, 500);
  }
}

export default { fetch: handle };
