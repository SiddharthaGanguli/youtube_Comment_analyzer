// The extension connection stage will call this helper after a user clicks Sign in.
// This module performs no network request and writes no token or secret to storage.
const REQUIRED_SCOPES = Object.freeze(["openid", "analysis/read", "analysis/write"]);
const MAX_SIGN_IN_AGE_MS = 5 * 60 * 1000;

function fail() { throw new Error("Invalid or expired sign-in attempt"); }

function parseUrl(value) {
  if (typeof value !== "string") fail();
  try { return new URL(value); } catch { fail(); }
}

function base64url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function randomValue() {
  return base64url(globalThis.crypto.getRandomValues(new Uint8Array(32)));
}

function checkedConfig(config) {
  if (!config || !/^[a-p]{32}$/.test(config.extensionId) || !/^[a-z0-9]{1,128}$/.test(config.clientId)) fail();
  const domain = parseUrl(config.cognitoDomain);
  if (!/^https:\/\/[a-z0-9]+(?:-[a-z0-9]+)*\.auth\.[a-z]{2}-[a-z]+-\d\.amazoncognito\.com\/?$/.test(config.cognitoDomain)
      || domain.username || domain.password || domain.search || domain.hash) fail();
  return { clientId: config.clientId, redirectUri: `https://${config.extensionId}.chromiumapp.org/cognito`,
    authorizeEndpoint: `${domain.origin}/oauth2/authorize`, tokenEndpoint: `${domain.origin}/oauth2/token` };
}

function equalState(actual, expected) {
  if (typeof actual !== "string" || typeof expected !== "string" || actual.length !== expected.length) return false;
  let mismatch = 0;
  for (let index = 0; index < actual.length; index++) mismatch |= actual.charCodeAt(index) ^ expected.charCodeAt(index);
  return mismatch === 0;
}

export async function pkceChallenge(verifier) {
  if (typeof verifier !== "string" || !/^[A-Za-z0-9._~-]{43,128}$/.test(verifier)) fail();
  const hash = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(hash));
}

export async function createAuthorizationRequest(config, nowMs = Date.now()) {
  if (!Number.isSafeInteger(nowMs) || nowMs < 0) fail();
  const checked = checkedConfig(config);
  const state = randomValue(), verifier = randomValue();
  const url = new URL(checked.authorizeEndpoint);
  url.search = new URLSearchParams({response_type: "code", client_id: checked.clientId, redirect_uri: checked.redirectUri,
    scope: REQUIRED_SCOPES.join(" "), state, code_challenge_method: "S256", code_challenge: await pkceChallenge(verifier)});
  return {url: url.href, pending: {state, verifier, createdAtMs: nowMs, clientId: checked.clientId,
    redirectUri: checked.redirectUri, tokenEndpoint: checked.tokenEndpoint, consumed: false}};
}

export function consumeAuthorizationCallback(callback, pending, nowMs = Date.now()) {
  if (!pending || pending.consumed !== false || !Number.isSafeInteger(nowMs) || !Number.isSafeInteger(pending.createdAtMs)
      || nowMs < pending.createdAtMs || nowMs - pending.createdAtMs > MAX_SIGN_IN_AGE_MS
      || !/^[A-Za-z0-9_-]{43}$/.test(pending.state) || !/^[A-Za-z0-9_-]{43}$/.test(pending.verifier)) fail();
  const expected = parseUrl(pending.redirectUri), result = parseUrl(callback);
  if (!/^https:\/\/[a-p]{32}\.chromiumapp\.org\/cognito$/.test(pending.redirectUri)
      || result.origin !== expected.origin || result.pathname !== expected.pathname || result.hash || result.username || result.password
      || result.searchParams.getAll("state").length !== 1 || !equalState(result.searchParams.get("state"), pending.state)) fail();
  const allowed = result.searchParams.has("error") ? ["state", "error", "error_description"] : ["state", "code"];
  if ([...result.searchParams.keys()].some(key => !allowed.includes(key)) ||
      [...new Set(result.searchParams.keys())].some(key => result.searchParams.getAll(key).length !== 1)) fail();
  const code = result.searchParams.get("code");
  if (!result.searchParams.has("error") && (!code || code.length > 2048 || /[\s\x00-\x1f\x7f]/.test(code))) fail();
  // Recheck the destination before preparing a request containing the verifier.
  const tokenUrl = parseUrl(pending.tokenEndpoint);
  const endpointConfig = checkedConfig({extensionId: expected.hostname.split(".")[0], clientId: pending.clientId,
                                        cognitoDomain: tokenUrl.origin});
  if (pending.tokenEndpoint !== endpointConfig.tokenEndpoint) fail();
  const verifier = pending.verifier;
  pending.consumed = true;
  pending.verifier = "";
  if (result.searchParams.has("error")) throw new Error("Sign-in was not completed");
  return {endpoint: endpointConfig.tokenEndpoint, options: {method: "POST", cache: "no-store", credentials: "omit", redirect: "error",
    headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: new URLSearchParams({grant_type: "authorization_code",
      client_id: pending.clientId, code, code_verifier: verifier, redirect_uri: pending.redirectUri}).toString()}};
}
