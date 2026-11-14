import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { handler } from "./index.mjs";

const c = { region: "ap-northeast-2", userPoolId: "pool", clientId: "client", domain: "login.example.com" };
const ev = (uri, { cookie, qs = "" } = {}) => {
  const headers = { host: [{ value: "d.cloudfront.net" }] };
  if (cookie) headers.cookie = [{ value: cookie }];
  return { Records: [{ cf: { request: { uri, querystring: qs, headers } } }] };
};
const okVerify = async t => (t === "good" ? { sub: "u" } : null);

test("unauthenticated page request redirects to Cognito with PKCE and a state cookie", async () => {
  const r = await handler(ev("/index.html", { qs: "split=dev" }), { config: c, verify: okVerify });
  assert.equal(r.status, "302");
  const loc = new URL(r.headers.location[0].value);
  assert.equal(loc.host, "login.example.com");
  assert.equal(loc.searchParams.get("code_challenge_method"), "S256");
  assert.equal(loc.searchParams.get("redirect_uri"), "https://d.cloudfront.net/_auth/callback");
  const st = JSON.parse(Buffer.from(r.headers["set-cookie"][0].value.split(";")[0].split("=")[1], "base64url"));
  assert.equal(st.state, loc.searchParams.get("state"));
  assert.equal(st.next, "/index.html?split=dev");
  assert.equal(loc.searchParams.get("code_challenge"), crypto.createHash("sha256").update(st.verifier).digest("base64url"));
});

test("valid id cookie passes the request through; data fetch without session gets 401", async () => {
  const req = await handler(ev("/data.json", { cookie: "x=1; arena_id=good" }), { config: c, verify: okVerify });
  assert.equal(req.uri, "/data.json");
  const r = await handler(ev("/data.json", { cookie: "arena_id=bad" }), { config: c, verify: okVerify });
  assert.equal(r.status, "401");
});

test("callback rejects state mismatch and exchanges code with the PKCE verifier", async () => {
  const stc = Buffer.from(JSON.stringify({ state: "s1", verifier: "v1", next: "//evil.com" })).toString("base64url");
  const bad = await handler(ev("/_auth/callback", { cookie: `arena_state=${stc}`, qs: "code=c&state=other" }), { config: c, verify: okVerify });
  assert.equal(bad.status, "400");
  let sent;
  const fetch = async (url, opts) => { sent = { url, body: new URLSearchParams(opts.body) }; return { ok: true, json: async () => ({ id_token: "good", expires_in: 3600 }) }; };
  const r = await handler(ev("/_auth/callback", { cookie: `arena_state=${stc}`, qs: "code=c&state=s1" }), { config: c, verify: okVerify, fetch });
  assert.equal(sent.url, "https://login.example.com/oauth2/token");
  assert.equal(sent.body.get("code_verifier"), "v1");
  assert.equal(r.status, "302");
  assert.equal(r.headers.location[0].value, "https://d.cloudfront.net/"); // open redirect "//evil.com" neutralised
  assert.match(r.headers["set-cookie"][0].value, /^arena_id=good; Path=\/; Secure; HttpOnly/);
});

test("logout clears the cookie and goes to Cognito logout", async () => {
  const r = await handler(ev("/_auth/logout"), { config: c, verify: okVerify });
  assert.match(r.headers.location[0].value, /^https:\/\/login\.example\.com\/logout\?client_id=client/);
  assert.match(r.headers["set-cookie"][0].value, /^arena_id=; .*Max-Age=0/);
});

test("verify checks RS256 signature, issuer, audience, token_use and expiry", async () => {
  const { verify } = await import("./index.mjs");
  const { privateKey, publicKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: "k1", alg: "RS256", use: "sig" };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ json: async () => ({ keys: [jwk] }) });
  const sign = (claims, key = privateKey) => {
    const h = Buffer.from(JSON.stringify({ alg: "RS256", kid: "k1" })).toString("base64url");
    const p = Buffer.from(JSON.stringify(claims)).toString("base64url");
    return `${h}.${p}.${crypto.sign("RSA-SHA256", Buffer.from(`${h}.${p}`), key).toString("base64url")}`;
  };
  const good = { iss: "https://cognito-idp.ap-northeast-2.amazonaws.com/pool", aud: "client", token_use: "id", exp: 2e9, email: "a@b.c" };
  try {
    assert.equal((await verify(sign(good), c, 1e9)).email, "a@b.c");
    assert.equal(await verify(sign({ ...good, exp: 1 }), c, 1e9), null);
    assert.equal(await verify(sign({ ...good, aud: "other" }), c, 1e9), null);
    assert.equal(await verify(sign({ ...good, token_use: "access" }), c, 1e9), null);
    const other = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 }).privateKey;
    assert.equal(await verify(sign(good, other), c, 1e9), null);
    assert.equal(await verify("not.a.jwt", c, 1e9), null);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("config failures answer fast with 503 instead of hitting the 5 s edge timeout", async () => {
  const t0 = Date.now();
  const r = await handler(ev("/"), { get config() { throw new Error("ssm down"); } });
  assert.equal(r.status, "503");
  assert.ok(Date.now() - t0 < 500);
});

test("SigV4 request for SSM is signed with the role session token", async () => {
  const { signSsmRequest } = await import("./index.mjs");
  const env = { AWS_ACCESS_KEY_ID: "AKIDEXAMPLE", AWS_SECRET_ACCESS_KEY: "secret", AWS_SESSION_TOKEN: "tok" };
  const { url, init } = signSsmRequest("/p", env, new Date("2026-11-14T00:00:00Z"));
  assert.equal(url, "https://ssm.us-east-1.amazonaws.com/");
  assert.equal(init.headers["x-amz-target"], "AmazonSSM.GetParameter");
  assert.equal(init.headers["x-amz-security-token"], "tok");
  assert.match(init.headers.authorization, /^AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE\/20261114\/us-east-1\/ssm\/aws4_request, SignedHeaders=content-type;host;x-amz-date;x-amz-security-token;x-amz-target, Signature=[0-9a-f]{64}$/);
  assert.equal(signSsmRequest("/p", env, new Date("2026-11-14T00:00:00Z")).init.headers.authorization, init.headers.authorization);
});
