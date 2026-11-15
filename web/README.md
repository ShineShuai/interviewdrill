# drill web

Cloudflare Worker (API) + D1 + PWA. Cards come from `../cards/*.md` (shared with the CLI).

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
