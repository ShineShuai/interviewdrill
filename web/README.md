# drill web

Cloudflare Worker (API) + D1 + PWA. Cards come from `../cards/*.md` (shared with the CLI),
plus approved problems and bug exercises (`../export_cards.py`).

## Deploy
```
npm i
npx wrangler d1 create drill          # paste database_id into wrangler.toml
npm run db:init && npm run cards
```
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

## Notes
- Daily cap (`DAILY_CAP`, default 200) counts LLM + STT calls per user per UTC day.
- Add a user: allow their email in Access. State is already per `user_id`.
- Grader prompt and `schedule()` mirror `drill.py`; keep both in sync or retire the CLI judge.

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
- CSP allows `wasm-unsafe-eval` for the runtime. If the browser console reports a CSP error
  about `eval`, the Pyodide build in use needs `'unsafe-eval'` as well; add it to `_headers`.
- Not for code you do not trust: the runner blocks `fetch` and friends but is not a sandbox
  against a hostile program.

## CLI sync (problem and bug progress)
`sync.py` shares attempts between the CLI and the web app: attempts are append-only events,
each side rebuilds a card's schedule by replaying them in time order, so devices converge.
`problems.py solve` and `bugs.py next` run it automatically when `DRILL_URL` is set.
1. Existing database: `npm run db:migrate-sync` once (adds `attempts.uid`; fresh ones have it).
2. Zero Trust > Access > Service auth: create a service token; add a policy with action
   "Service Auth" and that token to the same application. Copy Client ID and Client Secret.
3. `wrangler.toml`: `SYNC_CLIENT_ID` = the Client ID, `SYNC_USER` = your Access email. Deploy.
   The token may only call `/api/sync/*`, and acts as `SYNC_USER`.
4. CLI: `export DRILL_URL=https://<worker host> DRILL_CLIENT_ID=... DRILL_CLIENT_SECRET=...`
   then `./sync.py` (or `./sync.py --status`). `wrangler dev` needs no token.
Only `problem:` and `bug:` cards sync. A problem that is not published (`npm run cards`) stays
pending in the CLI and is retried on the next sync.

## Grading without an API key
Answers are graded by `LLM` in `wrangler.toml`:
- `workers-ai` (default): Cloudflare's own models through the `AI` binding. No key; the free
  plan has a daily allowance (10,000 "neurons", shared with speech-to-text). `LLM_MODEL` lists
  models to try in order; if the first is refused (some need the paid plan) or the allowance is
  used up, the next one answers, and if all fail the app offers "Grade myself".
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
