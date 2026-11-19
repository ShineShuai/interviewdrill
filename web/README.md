# interviewdrill web

Cloudflare Worker (API) + D1 + PWA. Cards come from `../cards/*.md` (shared with the CLI),
plus approved problems and bug exercises (`../export_cards.py`).

## Deploy
```
npm i
npx wrangler login
npx wrangler d1 create interviewdrill  # paste database_id into wrangler.toml
npm run db:init && npm run cards
npm run db:check                       # tables and columns of the remote database
```
`db:init` creates the current schema, so no migration applies to a database made with it. Run
`db:migrate-sync` only if `attempts` has no `uid` column and `db:migrate-mock` only if
`mock_sessions` is missing; `db:migrate-sync` on a current database fails with
`duplicate column name: uid`. `db:migrate-problems` matters only for a database that held `kata:` cards.
Create a Cloudflare Access (self-hosted) application covering the Worker hostname
(workers.dev too), allow your email, then set `TEAM_DOMAIN` and `ACCESS_AUD` in
`wrangler.toml`. The Worker returns 500 until both are set. Then `npm run deploy`.

## Local
`.dev.vars`: `ANTHROPIC_API_KEY=...` and `DEV_USER=me@localhost` (honored on localhost only).
```
npm run db:init:local && npm run cards:local && npm run dev
```
`/api/stt` uses the Workers AI binding, which runs remotely (needs `wrangler login`).

## Test
`npm test` (Node >= 22.18, no install needed) · `npm run typecheck` (after `npm i`).
Browser UI: `python3 -m unittest test_web_ui` from the repo root (needs `pip install playwright`
and `playwright install chromium`; skipped otherwise). It drives real Chromium against
`test/ui-server.ts`: the real Worker, security headers and cards, with Workers AI stubbed and
Pyodide stubbed except in `RealPyodideTest` (real runtime under the real CSP; needs `npm i`).

## Home-screen icons
iOS ignores SVG for the home-screen icon. `npm run icons` renders `public/icon.svg` into
`public/icons/*.png` (180 px Apple touch icon, 192/512 px, maskable 512 px) with resvg; it runs
before `npm run dev` and `npm run deploy`, and the folder is git-ignored. Keep the glyph as
paths: text would depend on the fonts of the machine that builds.

Real runtime: `npm run smoke` boots the Worker in workerd (`wrangler dev`) on a throwaway local
D1 and checks the API, the `_headers` rules and content types over HTTP (about 40 s; no login, no
network; the AI binding only runs remotely, so it is left out). `npx wrangler deploy --dry-run`
checks that the Worker bundles.

## Notes
- Daily cap (`DAILY_CAP`, default 200) counts LLM + STT calls per user per UTC day.
- Add a user: allow their email in Access. State is already per `user_id`.
- Grader prompt and `schedule()` mirror `drill.py`; keep both in sync or retire the CLI judge.

## Offline
Every time the home screen loads online, the app keeps the next 30 due cards (with references,
no problems) in `localStorage`. With no connection the home screen becomes "Offline": you drill
the cached cards and grade yourself against the checklist. Each answer goes into a queue
(`drill.queue`) and is sent through `POST /api/sync/push` when the browser is back online (or
on the next home screen). The queue keeps the time of the answer, and a retry never duplicates
an attempt (unique uid). A save that fails while "online" (flaky network, expired Access
session) is queued the same way. An attempt the server refuses is moved to `drill.rejected`.
Problems (they need the browser runner) and model grading are online only.

## Problems and bug exercises
`npm run cards` also publishes approved `problems/` and `bugs/` entries as cards with ids `problem:<id>`
and `bug:<id>` (topics `problems`, `bugs`; needs `python3`, override with `PYTHON`). The sync fails
if the export fails, because a silent skip would prune them from D1.
- Bugs: read the numbered C++ or Go, explain the class, cause and fix. Graded against the
  verified tool report (sanitizer, race detector or runtime) and diff.
- Problems: explain algorithm, complexity and edge cases (code optional). Graded against the verified
  reference solution and the known wrong approaches. Nothing is executed on the web; run
  `problems.py solve` locally for tests.
- The reference is shown after grading, never before.

## Python problems in the browser
Paste a Python solution (reads stdin, writes stdout) into a `problem:` card and press "Run as Python":
the verified tests run in a Web Worker with Pyodide, the network disabled, and a per-test time
limit of 5x the CLI limit (WebAssembly is slower). A runaway program is killed by terminating the
worker. The result (passed/total) updates the card's schedule like `problems.py solve` does.
- `npm install` fetches Pyodide (`^0.27`); `npm run vendor` copies it to `public/pyodide/`.
- `npm run problems` writes `public/problems/<id>.json` (tests) from `../problems`. Both run before
  `npm run dev` and `npm run deploy`, and both folders are git-ignored.
- The tests are plain static files, so the solution can peek at them; fine for one user.
- Tested with Pyodide 0.27.8 in Chromium: `wasm-unsafe-eval` is enough, `eval` stays refused, so
  the CSP needs no loosening.
- Not for code you do not trust: the runner blocks `fetch` and friends (also through the
  prototype chain) but is not a sandbox against a hostile program. Pyodide sometimes reports a
  blocked call as `SystemError` instead of the error text; the run still fails.

## Mock design interview
The Mock tab: a model plays the interviewer for a video-streaming (or your own) system-design
problem. It asks 2-12 lines, probes the weakest part of each answer, and after the last answer
(or "Finish and review") scores requirements, estimation, architecture, depth, trade-offs and
communication. Voice works like on cards (Record), and "Read the interviewer aloud" speaks the
lines. The transcript lives in the page; the server stores only the finished session in
`mock_sessions` (the 10 latest show under Recent; the Report tab adds the average per axis and
the weakest axis over its window, and the export includes the sessions).
- Existing database: `npm run db:migrate-mock` once (`db:init` databases have the table; check with `db:check`).
- Needs a grader (`workers-ai` or `anthropic`); there is no self-grading mode.
- Costs one LLM call per interviewer line plus one for the review (6 for the default 5 lines),
  counted against `DAILY_CAP`. The terminal version is `../mock_interview.py` (same prompts).

## CLI sync (cards, problems and bugs)
`sync.py` shares attempts between the CLI and the web app: attempts are append-only events,
each side rebuilds a card's schedule by replaying them in time order, so devices converge.
`drill.py daily`, `problems.py solve` and `bugs.py next` run it automatically when `DRILL_URL`
is set.
1. Existing database: `npm run db:migrate-sync` once (adds `attempts.uid`; `db:init` databases have it,
   and the migration then fails; check with `db:check`).
2. Zero Trust > Access > Service auth: create a service token; add a policy with action
   "Service Auth" and that token to the same application. Copy Client ID and Client Secret.
3. `wrangler.toml`: `SYNC_CLIENT_ID` = the Client ID, `SYNC_USER` = your Access email. Deploy.
   The token may only call `/api/sync/*`, and acts as `SYNC_USER`.
4. CLI: `export DRILL_URL=https://<worker host> DRILL_CLIENT_ID=... DRILL_CLIENT_SECRET=...`
   then `./sync.py` (or `./sync.py --status`). `wrangler dev` needs no token.
Every card syncs: scores, per-criterion grades and timing travel, answer texts stay on the device
where they were typed. The first sync uploads the whole local history. A card that is not
published (`npm run cards`) stays pending in the CLI and is retried on the next sync.
Deploy the Worker before running a newer CLI: an older Worker rejects card attempts.

## Grading without an API key
Answers are graded by `LLM` in `wrangler.toml`:
- `workers-ai` (default): Cloudflare's own models through the `AI` binding. No key; the free
  plan has a daily allowance (10,000 "neurons", shared with speech-to-text; Cloudflare's pricing
  page, Nov 2026). Both default models are open to the free plan; only newer models (Kimi, GLM,
  DeepSeek V4 ...) need paid billing. One graded answer costs roughly 60 neurons on the 70B model,
  so expect about 150 per day. `LLM_MODEL` lists models to try in order; if the first is refused
  or the allowance is used up, the next one answers, and if all fail the app offers "Grade
  myself". A model that refuses JSON mode is asked once more with the schema in the prompt.
- `anthropic`: `npx wrangler secret put ANTHROPIC_API_KEY`, set `LLM = "anthropic"`.
- `none`: no model. "Grade myself" shows the reference and a checklist; you tick what your
  answer covered and the share is the score (it is stored and scheduled like any other grade).
"Grade myself" is always there, whichever grader is on. Small models grade less reliably than
large ones; treat their scores as a hint.

## CLI without an API key (`llm.py`)
`DRILL_LLM` picks the provider; default: `anthropic` if `ANTHROPIC_API_KEY` is set, `openai` if
`DRILL_LLM_URL` is set, else `manual`.
- `openai`: any OpenAI-compatible endpoint: `DRILL_LLM_URL` (Ollama: `http://localhost:11434/v1`),
  `DRILL_LLM_MODEL`, `DRILL_LLM_KEY` if needed (Groq, OpenRouter, Google AI Studio, ...).
- `manual`: the tool prints the prompt (copied with `pbcopy` on macOS); paste it into claude.ai,
  paste the JSON reply back and end with a line saying `END`. Used for generating problems,
  bugs and cards. `gen.py run` skips its second model check there; quotes are still verified.
- Grading in the CLI (`drill.py daily`, `bugs.py next`) uses a model only if one is configured
  (or `DRILL_LLM=manual`); otherwise you tick a checklist yourself, as in the web app.
