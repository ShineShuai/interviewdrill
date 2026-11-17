export interface Env {
  DB: D1Database;
  AI: Ai;
  ANTHROPIC_API_KEY?: string; // only for LLM=anthropic: wrangler secret put ANTHROPIC_API_KEY
  TEAM_DOMAIN?: string; // https://<team>.cloudflareaccess.com
  ACCESS_AUD?: string; // Access application AUD tag
  DEV_USER?: string; // honored on localhost only (.dev.vars)
  SYNC_CLIENT_ID?: string; // Access service token (the CLI); empty disables machine access
  SYNC_USER?: string; // the user the service token acts as (your Access email)
  LLM?: string; // workers-ai (default) | anthropic | none
  LLM_MODEL?: string; // Workers AI model(s), comma separated, tried in order
  ANTHROPIC_MODEL?: string;
  DAILY_CAP?: string;
  STT_LANG?: string;
}
