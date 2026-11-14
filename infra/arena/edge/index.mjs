// Lambda@Edge viewer-request: Cognito Hosted UI login (authorization code + PKCE) for the Arena dashboard.
// No env vars are allowed on Lambda@Edge, so pool/client settings come from SSM (us-east-1), cached per container.
import crypto from "node:crypto";

const PARAM = "/s-trader-arena/auth-config";
const ID_COOKIE = "arena_id";
const STATE_COOKIE = "arena_state";
let cfg, jwks;

const TIMEOUT_MS = 2500; // viewer-request functions are capped at 5 s and 128 MB
const BUDGET_MS = 4200;   // total time allowed for the cold-start config lookup

// SSM GetParameter over HTTPS with a hand-made SigV4 signature. Importing the AWS SDK costs seconds
// at 128 MB, which made cold starts time out (503). Credentials come from the Lambda role env vars.
const hmac = (key, data) => crypto.createHmac("sha256", key).update(data).digest();
const sha256hex = data => crypto.createHash("sha256").update(data).digest("hex");

export function signSsmRequest(name, env = process.env, now = new Date(), region = "us-east-1") {
  const host = `ssm.${region}.amazonaws.com`, body = JSON.stringify({ Name: name });
  const amzDate = now.toISOString().replace(/[:-]|\.\d{3}/g, ""), day = amzDate.slice(0, 8);
  const headers = { "content-type": "application/x-amz-json-1.1", host, "x-amz-date": amzDate, "x-amz-target": "AmazonSSM.GetParameter" };
  if (env.AWS_SESSION_TOKEN) headers["x-amz-security-token"] = env.AWS_SESSION_TOKEN;
  const names = Object.keys(headers).sort();
  const canonical = ["POST", "/", "", names.map(k => `${k}:${headers[k]}\n`).join(""), names.join(";"), sha256hex(body)].join("\n");
  const scope = `${day}/${region}/ssm/aws4_request`;
  const toSign = ["AWS4-HMAC-SHA256", amzDate, scope, sha256hex(canonical)].join("\n");
  const key = ["aws4_request"].reduce((k, part) => hmac(k, part), hmac(hmac(hmac(`AWS4${env.AWS_SECRET_ACCESS_KEY}`, day), region), "ssm"));
  headers.authorization = `AWS4-HMAC-SHA256 Credential=${env.AWS_ACCESS_KEY_ID}/${scope}, SignedHeaders=${names.join(";")}, Signature=${crypto.createHmac("sha256", key).update(toSign).digest("hex")}`;
  delete headers.host; // fetch sends the same Host; it is a forbidden request header
  return { url: `https://${host}/`, init: { method: "POST", headers, body } };
}

// The parameter is written to us-east-1 and to the team's home region; read the copy in the region this
// edge replica runs in first (short TLS round trips), then fall back to us-east-1.
async function config() {
  if (!cfg) {
    const regions = [...new Set([process.env.AWS_REGION, "us-east-1"].filter(Boolean))];
    const deadline = Date.now() + BUDGET_MS; // whole lookup must finish before the 5 s edge limit
    let last;
    for (const region of regions) {
      const left = deadline - Date.now();
      if (left < 250) break;
      try {
        const { url, init } = signSsmRequest(PARAM, process.env, new Date(), region);
        const r = await fetch(url, { ...init, signal: AbortSignal.timeout(left) });
        if (!r.ok) throw new Error(`ssm ${region} ${r.status}`);
        cfg = JSON.parse((await r.json()).Parameter.Value); // { region, userPoolId, clientId, domain, jwks }
        if (cfg.jwks && !jwks) jwks = toKeys(cfg.jwks);
        return cfg;
      } catch (err) {
        last = err;
      }
    }
    throw last || new Error("config lookup budget exhausted");
  }
  return cfg;
}

const toKeys = body => Object.fromEntries(body.keys.map(k => [k.kid, crypto.createPublicKey({ key: k, format: "jwk" })]));

async function keys(c, refresh = false) {
  if (!jwks || refresh) {
    const r = await fetch(`https://cognito-idp.${c.region}.amazonaws.com/${c.userPoolId}/.well-known/jwks.json`, { signal: AbortSignal.timeout(TIMEOUT_MS) });
    jwks = toKeys(await r.json()); // only on unknown kid (key rotation) or when config carried no keys
  }
  return jwks;
}

const b64u = s => Buffer.from(s, "base64url");

export async function verify(token, c, now = Date.now() / 1000) {
  try {
    const [h, p, s] = String(token).split(".");
    if (!s) return null;
    const header = JSON.parse(b64u(h));
    const claims = JSON.parse(b64u(p));
    if (header.alg !== "RS256") return null;
    let key = (await keys(c))[header.kid];
    if (!key) key = (await keys(c, true))[header.kid]; // key rotation
    if (!key || !crypto.verify("RSA-SHA256", Buffer.from(`${h}.${p}`), key, b64u(s))) return null;
    const iss = `https://cognito-idp.${c.region}.amazonaws.com/${c.userPoolId}`;
    if (claims.iss !== iss || claims.aud !== c.clientId || claims.token_use !== "id" || !(claims.exp > now)) return null;
    return claims;
  } catch {
    return null;
  }
}

function parseCookies(headers) {
  const jar = {};
  for (const h of headers.cookie || []) {
    for (const part of h.value.split(";")) {
      const i = part.indexOf("=");
      if (i > 0) jar[part.slice(0, i).trim()] = part.slice(i + 1).trim();
    }
  }
  return jar;
}

const cookie = (name, value, maxAge) => `${name}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${maxAge}`;

function respond(status, { location, cookies = [], body, type = "text/plain; charset=utf-8" } = {}) {
  const headers = { "cache-control": [{ key: "Cache-Control", value: "no-store" }] };
  if (location) headers.location = [{ key: "Location", value: location }];
  if (cookies.length) headers["set-cookie"] = cookies.map(value => ({ key: "Set-Cookie", value }));
  if (body) headers["content-type"] = [{ key: "Content-Type", value: type }];
  return { status: String(status), headers, ...(body ? { body } : {}) };
}

export async function handler(event, deps = {}) {
  try {
    return await route(event, deps);
  } catch (err) {
    console.error("auth error", err && err.message);
    return respond(503, { body: "로그인 서비스에 일시적으로 연결하지 못했습니다. 잠시 후 새로고침하세요." });
  }
}

async function route(event, deps) {
  const req = event.Records[0].cf.request;
  const c = deps.config || (await config());
  const http = deps.fetch || fetch;
  const base = `https://${req.headers.host[0].value}`;
  const redirectUri = `${base}/_auth/callback`;
  const jar = parseCookies(req.headers);
  const qs = new URLSearchParams(req.querystring || "");

  if (req.uri === "/_auth/callback") {
    let st = null;
    try { st = JSON.parse(b64u(jar[STATE_COOKIE] || "")); } catch { st = null; }
    if (!st || !qs.get("code") || qs.get("state") !== st.state) {
      return respond(400, { body: "로그인 상태가 맞지 않습니다. 대시보드 주소로 다시 접속하세요." });
    }
    const r = await http(`https://${c.domain}/oauth2/token`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "authorization_code", client_id: c.clientId, code: qs.get("code"),
                                  redirect_uri: redirectUri, code_verifier: st.verifier }).toString(),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!r.ok) return respond(502, { body: "토큰 교환에 실패했습니다. 다시 시도하세요." });
    const t = await r.json();
    if (!(await (deps.verify || verify)(t.id_token, c))) return respond(401, { body: "토큰 검증 실패" });
    const next = typeof st.next === "string" && st.next.startsWith("/") && !st.next.startsWith("//") ? st.next : "/";
    return respond(302, { location: base + next, cookies: [cookie(ID_COOKIE, t.id_token, t.expires_in || 3600), cookie(STATE_COOKIE, "", 0)] });
  }

  if (req.uri === "/_auth/logout") {
    const to = `https://${c.domain}/logout?` + new URLSearchParams({ client_id: c.clientId, logout_uri: `${base}/_auth/signed-out` });
    return respond(302, { location: to, cookies: [cookie(ID_COOKIE, "", 0)] });
  }

  if (req.uri === "/_auth/signed-out") {
    return respond(200, { type: "text/html; charset=utf-8", body: '<!doctype html><meta charset="utf-8"><p>로그아웃되었습니다. <a href="/">다시 로그인</a></p>' });
  }

  if (jar[ID_COOKIE] && (await (deps.verify || verify)(jar[ID_COOKIE], c))) return req;

  if (req.uri.endsWith(".json")) return respond(401, { body: "session expired" }); // fetch() cannot follow a cross-origin login redirect

  const verifier = crypto.randomBytes(32).toString("base64url");
  const state = crypto.randomBytes(16).toString("base64url");
  const challenge = crypto.createHash("sha256").update(verifier).digest("base64url");
  const next = req.uri + (req.querystring ? `?${req.querystring}` : "");
  const login = `https://${c.domain}/oauth2/authorize?` + new URLSearchParams({
    response_type: "code", client_id: c.clientId, redirect_uri: redirectUri, scope: "openid email profile",
    state, code_challenge: challenge, code_challenge_method: "S256",
  });
  const stateCookie = Buffer.from(JSON.stringify({ state, verifier, next })).toString("base64url");
  return respond(302, { location: login, cookies: [cookie(STATE_COOKIE, stateCookie, 600)] });
}
