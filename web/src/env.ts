export interface Env {
  DB: D1Database;
  AI: Ai;
  ANTHROPIC_API_KEY: string;
  TEAM_DOMAIN?: string; // https://<team>.cloudflareaccess.com
  ACCESS_AUD?: string; // Access application AUD tag
  DEV_USER?: string; // honored on localhost only (.dev.vars)
  MODEL?: string;
  DAILY_CAP?: string;
  STT_LANG?: string;
}
