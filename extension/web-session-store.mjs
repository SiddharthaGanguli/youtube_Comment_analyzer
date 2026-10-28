import {validateWebClientConfig} from "./web-client-config.mjs";

const VERSION = 1;
const MAX_TOKEN_BYTES = 16_384;
const MAX_RECORD_BYTES = 20_480;
const MAX_SESSION_MS = 60 * 60 * 1000;
export const WEB_SESSION_SKEW_MS = 30_000;
const RECORD_FIELDS = "accessToken,clientId,cognitoDomain,expiresAtMs,redirectUri,storedAtMs,version";

function unavailable() {
  return Object.assign(new Error("Secure website session storage is unavailable."), {code: "AUTH_UNAVAILABLE"});
}

function context(config, storage, now) {
  try {
    const checked = validateWebClientConfig(config);
    if (!checked.enabled) return null;
    const target = storage ?? globalThis.sessionStorage;
    if (!target || typeof target.getItem !== "function" || typeof target.removeItem !== "function") throw unavailable();
    const timestamp = now();
    if (!Number.isSafeInteger(timestamp) || timestamp < 0) throw unavailable();
    const key = "commentLens.webSession.v1:" + JSON.stringify([checked.cognitoDomain, checked.clientId, checked.redirectUri]);
    return {config: checked, storage: target, now: timestamp, key};
  } catch { throw unavailable(); }
}

function tokenExpiry(token, config, now) {
  if (typeof token !== "string" || token.length > MAX_TOKEN_BYTES
      || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) throw unavailable();
  const encoded = token.split(".")[1];
  const bytes = Uint8Array.from(atob(encoded.replaceAll("-", "+").replaceAll("_", "/")), character => character.charCodeAt(0));
  const payload = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));
  const expiry = payload?.exp * 1000;
  if (!payload || Array.isArray(payload) || payload.token_use !== "access" || payload.client_id !== config.clientId
      || typeof payload.scope !== "string" || payload.scope.length > 2048
      || !["analysis/read", "analysis/write"].every(scope => payload.scope.split(" ").includes(scope))
      || !Number.isSafeInteger(payload.exp) || !Number.isSafeInteger(expiry) || expiry <= now + WEB_SESSION_SKEW_MS) throw unavailable();
  // Decoded claims bound local display/session behavior. API Gateway and the
  // application still verify the signature, issuer, client and scopes.
  return expiry;
}

function sessionFrom(record, target) {
  if (!record || typeof record !== "object" || Array.isArray(record)
      || Object.keys(record).sort().join(",") !== RECORD_FIELDS || record.version !== VERSION
      || record.clientId !== target.config.clientId || record.cognitoDomain !== target.config.cognitoDomain
      || record.redirectUri !== target.config.redirectUri || !Number.isSafeInteger(record.storedAtMs)
      || record.storedAtMs < 0 || record.storedAtMs > target.now || !Number.isSafeInteger(record.expiresAtMs)
      || record.expiresAtMs <= target.now + WEB_SESSION_SKEW_MS || record.expiresAtMs > record.storedAtMs + MAX_SESSION_MS
      || record.expiresAtMs > tokenExpiry(record.accessToken, target.config, target.now)) throw unavailable();
  return Object.freeze({accessToken: record.accessToken, expiresAtMs: record.expiresAtMs});
}

export function readWebSession(config, {storage, now = Date.now} = {}) {
  const target = context(config, storage, now);
  if (!target) return null;
  let raw;
  try { raw = target.storage.getItem(target.key); } catch { throw unavailable(); }
  if (raw === null) return null;
  try {
    if (typeof raw !== "string" || raw.length > MAX_RECORD_BYTES) throw unavailable();
    return sessionFrom(JSON.parse(raw), target);
  } catch {
    try { target.storage.removeItem(target.key); } catch { throw unavailable(); }
    return null;
  }
}

export function writeWebSession(config, session, {storage, now = Date.now} = {}) {
  const target = context(config, storage, now);
  if (!target) throw unavailable();
  try {
    if (!session || typeof session !== "object" || Array.isArray(session)
        || Object.keys(session).sort().join(",") !== "accessToken,expiresAtMs"
        || typeof target.storage.setItem !== "function" || !Number.isSafeInteger(session.expiresAtMs)) throw unavailable();
    const record = {version: VERSION, clientId: target.config.clientId, cognitoDomain: target.config.cognitoDomain,
      redirectUri: target.config.redirectUri, accessToken: session.accessToken, storedAtMs: target.now,
      expiresAtMs: Math.min(session.expiresAtMs, target.now + MAX_SESSION_MS)};
    const checked = sessionFrom(record, target), raw = JSON.stringify(record);
    if (raw.length > MAX_RECORD_BYTES) throw unavailable();
    target.storage.setItem(target.key, raw);
    if (target.storage.getItem(target.key) !== raw) throw unavailable();
    return checked;
  } catch {
    try { target.storage.removeItem(target.key); } catch { /* Preserve a fixed, safe failure. */ }
    throw unavailable();
  }
}

export function clearWebSession(config, {storage} = {}) {
  const target = context(config, storage, Date.now);
  if (!target) return;
  try { target.storage.removeItem(target.key); } catch { throw unavailable(); }
}
