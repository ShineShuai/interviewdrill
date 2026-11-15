import type { Env } from "./env.ts";
import { HttpError } from "./http.ts";

interface Claims {
  iss?: string;
  aud?: string | string[];
  exp?: number;
  nbf?: number;
  email?: string;
  sub?: string;
  common_name?: string; // the client id of a service token
}

const ALG = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" } as const;
const KEY_TTL_MS = 3_600_000;
const REFRESH_MIN_MS = 60_000;
const cache = new Map<string, { at: number; keys: Map<string, CryptoKey> }>();

const b64 = (s: string): Uint8Array<ArrayBuffer> =>
  Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
const utf8 = (s: string): string => new TextDecoder().decode(b64(s));

async function loadKeys(team: string): Promise<Map<string, CryptoKey>> {
  const res = await fetch(`${team}/cdn-cgi/access/certs`);
  if (!res.ok) throw new Error(`access certs: ${res.status}`);
  const { keys } = (await res.json()) as { keys: (JsonWebKey & { kid: string })[] };
  const out = new Map<string, CryptoKey>();
  for (const k of keys.filter((k) => k.kty === "RSA")) {
    out.set(k.kid, await crypto.subtle.importKey("jwk", k, ALG, false, ["verify"]));
  }
  return out;
}

async function getKey(team: string, kid: string): Promise<CryptoKey | undefined> {
  const hit = cache.get(team);
  const age = hit ? Date.now() - hit.at : Infinity;
  if (hit && age < KEY_TTL_MS && (hit.keys.has(kid) || age < REFRESH_MIN_MS)) {
    return hit.keys.get(kid);
  }
  const fresh = { at: Date.now(), keys: await loadKeys(team) };
  cache.set(team, fresh);
  return fresh.keys.get(kid);
}

/** Verify a Cloudflare Access JWT (RS256, issuer, audience, expiry). Null if invalid. */
export async function verifyAccessJwt(
  token: string,
  team: string,
  aud: string,
): Promise<Claims | null> {
  const [h, p, s] = token.split(".");
  if (!h || !p || !s) return null;
  let header: { alg?: string; kid?: string };
  let claims: Claims;
  try {
    header = JSON.parse(utf8(h));
    claims = JSON.parse(utf8(p));
  } catch {
    return null;
  }
  if (header.alg !== "RS256" || !header.kid) return null;
  const key = await getKey(team, header.kid);
  if (!key) return null;
  const data = new TextEncoder().encode(`${h}.${p}`);
  if (!(await crypto.subtle.verify(ALG.name, key, b64(s), data))) return null;
  const now = Date.now() / 1000;
  const auds = [claims.aud ?? []].flat();
  if (claims.iss !== team || !auds.includes(aud)) return null;
  if (!(typeof claims.exp === "number" && claims.exp > now)) return null;
  if (typeof claims.nbf === "number" && claims.nbf > now + 60) return null;
  return claims;
}

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

export interface Identity {
  user: string;
  machine: boolean; // a service token: may only use the sync routes
}

/** Who is calling, or null if unauthenticated. Fails closed if auth is not configured. */
export async function authenticate(req: Request, env: Env): Promise<Identity | null> {
  if (env.DEV_USER && LOCAL_HOSTS.has(new URL(req.url).hostname)) {
    return { user: env.DEV_USER, machine: false };
  }
  if (!env.TEAM_DOMAIN || !env.ACCESS_AUD) throw new HttpError(500, "auth not configured");
  const token = req.headers.get("Cf-Access-Jwt-Assertion");
  if (!token) return null;
  const claims = await verifyAccessJwt(token, env.TEAM_DOMAIN.replace(/\/$/, ""), env.ACCESS_AUD);
  if (!claims) return null;
  if (claims.email) return { user: claims.email, machine: false };
  if (claims.common_name) {
    // Only the one configured service token is accepted, and it acts as one fixed user.
    const known = env.SYNC_CLIENT_ID && env.SYNC_USER && claims.common_name === env.SYNC_CLIENT_ID;
    return known ? { user: env.SYNC_USER as string, machine: true } : null;
  }
  return claims.sub ? { user: claims.sub, machine: false } : null;
}
