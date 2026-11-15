# drill web

Cloudflare Worker (API) + D1 + PWA. Cards come from `../cards/*.md` (shared with the CLI),
plus approved katas and bug exercises (`../export_cards.py`).

## Deploy
```
npm i
npx wrangler d1 create drill          # paste database_id into wrangler.toml
npm run db:init && npm run cards
npx wrangler secret put ANTHROPIC_API_KEY
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

## Katas and bug exercises
`npm run cards` also publishes approved `katas/` and `bugs/` entries as cards with ids `kata:<id>`
and `bug:<id>` (topics `katas`, `bugs`; needs `python3`, override with `PYTHON`). The sync fails
if the export fails, because a silent skip would prune them from D1.
- Bugs: read the numbered C++ or Go, explain the class, cause and fix. Graded against the
  verified tool report (sanitizer, race detector or runtime) and diff.
- Katas: explain algorithm, complexity and edge cases (code optional). Graded against the verified
  reference solution and the known wrong approaches. Nothing is executed on the web; run
  `kata.py solve` locally for tests.
- The reference is shown after grading, never before.
