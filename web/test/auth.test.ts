import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { authenticate, verifyAccessJwt } from "../src/auth.ts";
import type { Env } from "../src/env.ts";
import { handle } from "../src/index.ts";
import { fakeD1 } from "./fake-d1.ts";

const TEAM = "https://team.cloudflareaccess.com";
const AUD = "aud-tag";
const RSA = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, hash: "SHA-256" };
const real = globalThis.fetch;
let pair: CryptoKeyPair;
let other: CryptoKeyPair;

const b64u = (b: ArrayBuffer | string): string =>
  Buffer.from(typeof b === "string" ? b : new Uint8Array(b)).toString("base64url");

async function token(claims: object, signer = () => pair.privateKey): Promise<string> {
  const head = b64u(JSON.stringify({ alg: "RS256", kid: "k1" }));
  const body = b64u(JSON.stringify(claims));
  const data = Buffer.from(`${head}.${body}`);
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", signer(), data);
  return `${head}.${body}.${b64u(sig)}`;
}

const good = () => ({ iss: TEAM, aud: [AUD], exp: Date.now() / 1000 + 600, email: "me@x.org" });

before(async () => {
  const gen = (): Promise<CryptoKeyPair> =>
    crypto.subtle.generateKey({ ...RSA, publicExponent: new Uint8Array([1, 0, 1]) }, true, [
      "sign",
      "verify",
    ]) as Promise<CryptoKeyPair>;
  pair = await gen();
  other = await gen();
  const jwk = { ...(await crypto.subtle.exportKey("jwk", pair.publicKey)), kid: "k1" };
  globalThis.fetch = (async () => Response.json({ keys: [jwk] })) as typeof fetch;
});
after(() => {
  globalThis.fetch = real;
});

test("accepts a valid token", async () => {
  const c = await verifyAccessJwt(await token(good()), TEAM, AUD);
  assert.equal(c?.email, "me@x.org");
});

test("rejects wrong audience, issuer, expiry, signature, garbage", async () => {
  assert.equal(await verifyAccessJwt(await token({ ...good(), aud: ["nope"] }), TEAM, AUD), null);
  const evil = await token({ ...good(), iss: "https://evil" });
  assert.equal(await verifyAccessJwt(evil, TEAM, AUD), null);
  assert.equal(await verifyAccessJwt(await token({ ...good(), exp: 1 }), TEAM, AUD), null);
  const forged = await token(good(), () => other.privateKey);
  assert.equal(await verifyAccessJwt(forged, TEAM, AUD), null);
  assert.equal(await verifyAccessJwt("a.b.c", TEAM, AUD), null);
  assert.equal(await verifyAccessJwt("nope", TEAM, AUD), null);
});

test("authenticate: header required, fails closed, DEV_USER only on localhost", async () => {
  const env = { TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD } as Env;
  const req = (host: string, headers: Record<string, string> = {}) =>
    new Request(`https://${host}/api/status`, { headers });
  assert.equal(await authenticate(req("app.example.com"), env), null);
  const jwt = await token(good());
  const authed = req("app.example.com", { "Cf-Access-Jwt-Assertion": jwt });
  assert.deepEqual(await authenticate(authed, env), { user: "me@x.org", machine: false });
  await assert.rejects(authenticate(req("app.example.com"), {} as Env), { status: 500 });
  const dev = { DEV_USER: "dev" } as Env;
  assert.deepEqual(await authenticate(req("localhost"), dev), { user: "dev", machine: false });
  await assert.rejects(authenticate(req("app.example.com"), dev), { status: 500 });
});

test("service tokens: only the configured client id, acting as the configured user", async () => {
  const env = {
    TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD, SYNC_CLIENT_ID: "abc.access", SYNC_USER: "me@x.org",
  } as Env;
  const as = async (claims: object, e: Env = env) => {
    const jwt = await token({ iss: TEAM, aud: [AUD], exp: Date.now() / 1000 + 600, ...claims });
    const req = new Request("https://app.example.com/api/sync/pull", {
      headers: { "Cf-Access-Jwt-Assertion": jwt },
    });
    return authenticate(req, e);
  };
  assert.deepEqual(await as({ common_name: "abc.access", sub: "" }), {
    user: "me@x.org", machine: true,
  });
  assert.equal(await as({ common_name: "other.access" }), null); // unknown service token
  const off = (extra: object) => ({ ...env, ...extra }) as Env;
  assert.equal(await as({ common_name: "abc.access" }, off({ SYNC_CLIENT_ID: "" })), null);
  assert.equal(await as({ common_name: "abc.access" }, off({ SYNC_USER: "" })), null);
  // a person is never treated as a machine, even if the claims also carry a common name
  assert.deepEqual(await as({ email: "p@x.org", common_name: "abc.access" }), {
    user: "p@x.org", machine: false,
  });
  assert.deepEqual(await as({ sub: "user-uuid" }), { user: "user-uuid", machine: false });
  assert.equal(await as({}), null);
});

test("a service token reaches the sync routes as SYNC_USER and nothing else", async () => {
  const db = fakeD1();
  db.raw.exec("INSERT INTO cards VALUES('problem:a','problems','Q','- r')");
  const env = {
    DB: db as unknown as Env["DB"], TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD,
    SYNC_CLIENT_ID: "abc.access", SYNC_USER: "me@x.org", ANTHROPIC_API_KEY: "k",
  } as Env;
  const jwt = await token({
    iss: TEAM, aud: [AUD], exp: Date.now() / 1000 + 600, common_name: "abc.access", sub: "",
  });
  const call = (path: string, init: RequestInit = {}) =>
    handle(
      new Request(`https://app.example.com${path}`, {
        ...init,
        headers: {
          "Cf-Access-Jwt-Assertion": jwt, "X-Drill": "1", "Content-Type": "application/json",
        },
      }),
      env,
    );
  const body = JSON.stringify({
    attempts: [
      { uid: "d:1", key: "problem:a", ts: "2026-11-01T09:00:00Z", q: 5, passed: 1, total: 1 },
    ],
  });
  const pushed = await call("/api/sync/push", { method: "POST", body });
  assert.equal(pushed.status, 200);
  const row = db.raw.prepare("SELECT user_id FROM attempts").get() as { user_id: string };
  assert.equal(row.user_id, "me@x.org");
  assert.equal((await call("/api/sync/pull")).status, 200);
  for (const [path, init] of [
    ["/api/status", {}], ["/api/export", {}],
    ["/api/attempt", { method: "POST", body: "{}" }], ["/api/stt", { method: "POST", body: "x" }],
    ["/api/problem/result", { method: "POST", body: "{}" }],
  ] as [string, RequestInit][]) {
    assert.equal((await call(path, init)).status, 403, path);
  }
  const anon = await handle(new Request("https://app.example.com/api/sync/pull"), env);
  assert.equal(anon.status, 401);
});
