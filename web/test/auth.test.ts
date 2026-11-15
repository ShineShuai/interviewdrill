import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { authenticate, verifyAccessJwt } from "../src/auth.ts";
import type { Env } from "../src/env.ts";

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
  assert.equal(await authenticate(authed, env), "me@x.org");
  await assert.rejects(authenticate(req("app.example.com"), {} as Env), { status: 500 });
  const dev = { DEV_USER: "dev" } as Env;
  assert.equal(await authenticate(req("localhost"), dev), "dev");
  await assert.rejects(authenticate(req("app.example.com"), dev), { status: 500 });
});
