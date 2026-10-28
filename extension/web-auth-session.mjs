import {pkceChallenge} from "./auth-pkce.mjs";
import {validateWebClientConfig} from "./web-client-config.mjs";

const SIGN_IN_AGE_MS = 5 * 60 * 1000;
const SKEW_MS = 30_000;
const TOKEN_TIMEOUT_MS = 10_000;
const MAX_TOKEN_BYTES = 16_384;
const MAX_RESPONSE_BYTES = 65_536;
const MAX_LIFETIME_SECONDS = 86_400;
const SCOPES = ["analysis/read", "analysis/write"];
const CALLBACK_PARAMETERS = new Set(["code", "state", "error", "error_description"]);
const PRIVATE_PARAMETERS = new Set([...CALLBACK_PARAMETERS, "access_token", "id_token", "refresh_token", "token_type", "expires_in", "scope"]);

function failure(code = "AUTH_FAILED") {
  const error = new Error(code === "AUTH_REQUIRED" ? "Sign in to continue." :
    code === "AUTH_CANCELLED" ? "Sign-in was cancelled." :
    code === "AUTH_UNAVAILABLE" ? "Secure website sign-in is unavailable." : "Sign-in could not be completed. Please try again.");
  error.code = code;
  return error;
}

function randomValue() {
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function equalState(actual, expected) {
  if (typeof actual !== "string" || actual.length !== expected.length) return false;
  let difference = 0;
  for (let index = 0; index < actual.length; index++) difference |= actual.charCodeAt(index) ^ expected.charCodeAt(index);
  return difference === 0;
}

function abortable(value, signal) {
  if (signal.aborted) return Promise.reject(failure("AUTH_CANCELLED"));
  return new Promise((resolve, reject) => {
    const abort = () => reject(failure("AUTH_CANCELLED"));
    signal.addEventListener("abort", abort, {once: true});
    Promise.resolve(value).then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}

function requiredScopes(value) {
  return typeof value === "string" && value.length <= 2048 && SCOPES.every(scope => value.split(" ").includes(scope));
}

function claims(token) {
  if (typeof token !== "string" || token.length > MAX_TOKEN_BYTES || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) throw failure();
  try {
    const encoded = token.split(".")[1];
    const bytes = Uint8Array.from(atob(encoded.replaceAll("-", "+").replaceAll("_", "/")), character => character.charCodeAt(0));
    return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));
  } catch { throw failure(); }
}

async function readTokenResponse(response, endpoint, signal) {
  if (!response?.ok || response.redirected || response.status !== 200 || response.url && response.url !== endpoint) throw failure();
  const length = response.headers?.get("Content-Length");
  if (length !== null && length !== undefined && (!/^\d{1,8}$/.test(length) || Number(length) > MAX_RESPONSE_BYTES)) throw failure();
  if (response.headers?.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json"
      || !response.body?.getReader) throw failure();
  const reader = response.body.getReader(), chunks = [];
  let size = 0;
  try {
    while (true) {
      const {done, value} = await abortable(reader.read(), signal);
      if (done) break;
      if (!(value instanceof Uint8Array) || (size += value.byteLength) > MAX_RESPONSE_BYTES) throw failure();
      chunks.push(value);
    }
    const body = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    const data = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(body));
    if (!data || typeof data !== "object" || Array.isArray(data)) throw failure();
    return data;
  } finally {
    try { reader.cancel().catch(() => {}); } catch { /* Best effort when a stream already failed. */ }
    try { reader.releaseLock(); } catch { /* An aborted read may still be settling. */ }
  }
}

// This session deliberately keeps tokens in memory. Only the short-lived PKCE
// attempt crosses the full-page redirect, using this tab's sessionStorage.
// Reloading requires a fresh user sign-in; there is no refresh-token persistence.
export class WebAuthSession {
  #config; #location; #history; #storage; #fetch; #now; #navigate; #key;
  #session = null; #initialized = false; #initializing = null; #signing = null;
  #active = null; #disposed = false; #generation = 0;

  constructor({config, locationApi = globalThis.location, historyApi = globalThis.history,
    storage = globalThis.sessionStorage, fetchImpl = globalThis.fetch?.bind(globalThis), now = Date.now,
    navigate = url => locationApi.assign(url)}) {
    try { this.#config = validateWebClientConfig(config); } catch { throw failure("AUTH_UNAVAILABLE"); }
    if (!this.#config.enabled || typeof fetchImpl !== "function" || typeof now !== "function"
        || typeof navigate !== "function" || !historyApi?.replaceState || !storage?.getItem || !storage?.setItem || !storage?.removeItem) throw failure("AUTH_UNAVAILABLE");
    this.#location = locationApi; this.#history = historyApi; this.#storage = storage;
    this.#fetch = fetchImpl; this.#now = now; this.#navigate = navigate;
    this.#key = `commentLens.webPkce.${this.#config.clientId}.${this.#config.redirectUri}`;
    this.#assertPage();
  }

  #clock() {
    const now = this.#now();
    if (!Number.isSafeInteger(now) || now < 0) throw failure("AUTH_UNAVAILABLE");
    return now;
  }

  #assertPage() {
    if (this.#disposed) throw failure("AUTH_UNAVAILABLE");
    let current;
    try { current = new URL(this.#location.href); } catch { throw failure("AUTH_UNAVAILABLE"); }
    const expected = new URL(this.#config.redirectUri);
    if (current.origin !== expected.origin || current.pathname !== expected.pathname || current.username || current.password) throw failure("AUTH_UNAVAILABLE");
    return current;
  }

  #removePending() {
    try { this.#storage.removeItem(this.#key); } catch { throw failure("AUTH_UNAVAILABLE"); }
  }

  #takePending() {
    let raw;
    try { raw = this.#storage.getItem(this.#key); this.#storage.removeItem(this.#key); }
    catch { throw failure("AUTH_UNAVAILABLE"); }
    if (typeof raw !== "string" || raw.length > 2048) throw failure();
    let pending;
    try { pending = JSON.parse(raw); } catch { throw failure(); }
    const now = this.#clock();
    if (!pending || typeof pending !== "object" || Array.isArray(pending)
        || Object.keys(pending).sort().join(",") !== "createdAtMs,state,verifier"
        || typeof pending.state !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(pending.state)
        || typeof pending.verifier !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(pending.verifier)
        || !Number.isSafeInteger(pending.createdAtMs) || now < pending.createdAtMs || now - pending.createdAtMs > SIGN_IN_AGE_MS) throw failure();
    return pending;
  }

  get signedIn() { return !this.#disposed && !!this.#session && this.#session.expiresAtMs > this.#clock() + SKEW_MS; }

  initialize() {
    this.#assertPage();
    if (this.#initialized) return Promise.resolve(this.signedIn);
    if (this.#initializing) return this.#initializing;
    const generation = this.#generation;
    this.#initializing = (async () => {
      const current = this.#assertPage();
      const fragment = new URLSearchParams(current.hash.slice(1));
      const callback = [...fragment.keys()].some(key => PRIVATE_PARAMETERS.has(key))
        || [...current.searchParams.keys()].some(key => PRIVATE_PARAMETERS.has(key));
      if (!callback) { this.#initialized = true; return false; }
      // Remove OAuth codes, state, tokens and provider error text from browser
      // history before any network operation or UI error can display them.
      const clean = new URL(current);
      for (const name of PRIVATE_PARAMETERS) clean.searchParams.delete(name);
      if ([...fragment.keys()].some(key => PRIVATE_PARAMETERS.has(key))) {
        for (const name of PRIVATE_PARAMETERS) fragment.delete(name);
        clean.hash = fragment.toString();
      }
      try { this.#history.replaceState(null, "", clean.href); }
      catch { this.#removePending(); throw failure("AUTH_UNAVAILABLE"); }
      let pending;
      const controller = new AbortController();
      this.#active = controller;
      let timer;
      try {
        pending = this.#takePending();
        const parameters = current.searchParams;
        if (current.hash || [...parameters.keys()].some(key => !CALLBACK_PARAMETERS.has(key))
            || [...new Set(parameters.keys())].some(key => parameters.getAll(key).length !== 1)
            || !equalState(parameters.get("state"), pending.state)) throw failure();
        if (parameters.has("error")) throw failure();
        const code = parameters.get("code");
        if (!code || code.length > 2048 || /[\s\x00-\x1f\x7f]/.test(code) || parameters.has("error_description")) throw failure();
        const endpoint = `${this.#config.cognitoDomain}/oauth2/token`;
        const body = new URLSearchParams({grant_type: "authorization_code", client_id: this.#config.clientId,
          code, code_verifier: pending.verifier, redirect_uri: this.#config.redirectUri}).toString();
        pending.verifier = "";
        timer = globalThis.setTimeout(() => controller.abort(), TOKEN_TIMEOUT_MS);
        const response = await abortable(this.#fetch(endpoint, {method: "POST", credentials: "omit", redirect: "error", cache: "no-store",
          headers: {"Content-Type": "application/x-www-form-urlencoded"}, body, signal: controller.signal}), controller.signal);
        const data = await readTokenResponse(response, endpoint, controller.signal);
        if (typeof data.token_type !== "string" || data.token_type.toLowerCase() !== "bearer"
            || !Number.isSafeInteger(data.expires_in) || data.expires_in <= SKEW_MS / 1000 || data.expires_in > MAX_LIFETIME_SECONDS
            || data.scope !== undefined && !requiredScopes(data.scope)) throw failure();
        // These decoded claims only bound browser behavior; the backend still
        // validates the JWT signature, issuer, client and scopes on each call.
        const payload = claims(data.access_token), now = this.#clock();
        if (payload.token_use !== "access" || payload.client_id !== this.#config.clientId || !requiredScopes(payload.scope)
            || !Number.isSafeInteger(payload.exp) || payload.exp * 1000 <= now + SKEW_MS) throw failure();
        if (controller.signal.aborted || generation !== this.#generation || this.#disposed) throw failure("AUTH_CANCELLED");
        this.#session = {accessToken: data.access_token, expiresAtMs: Math.min(now + data.expires_in * 1000, payload.exp * 1000)};
        return true;
      } catch (error) {
        this.#session = null;
        throw failure(controller.signal.aborted || error?.code === "AUTH_CANCELLED" ? "AUTH_CANCELLED" :
          error?.code === "AUTH_UNAVAILABLE" ? "AUTH_UNAVAILABLE" : "AUTH_FAILED");
      } finally {
        globalThis.clearTimeout(timer);
        if (pending) pending.verifier = "";
        if (this.#active === controller) this.#active = null;
        this.#initialized = true;
      }
    })().finally(() => { this.#initializing = null; });
    return this.#initializing;
  }

  async getAccessToken() {
    await this.initialize();
    if (!this.signedIn) { this.#session = null; throw failure("AUTH_REQUIRED"); }
    return this.#session.accessToken;
  }

  signIn() { return this.#authorize("/oauth2/authorize"); }
  signUp() { return this.#authorize("/signup"); }

  #authorize(path) {
    if (this.#signing) return this.#signing;
    const generation = this.#generation, controller = new AbortController();
    this.#signing = (async () => {
      let leaving = false;
      try {
        await this.initialize();
        this.#assertPage();
        if (generation !== this.#generation) throw failure("AUTH_CANCELLED");
        this.#active = controller;
        const pending = {state: randomValue(), verifier: randomValue(), createdAtMs: this.#clock()};
        const challenge = await abortable(pkceChallenge(pending.verifier), controller.signal);
        if (generation !== this.#generation) throw failure("AUTH_CANCELLED");
        this.#storage.setItem(this.#key, JSON.stringify(pending));
        this.#session = null;
        const url = new URL(`${this.#config.cognitoDomain}${path}`);
        url.search = new URLSearchParams({response_type: "code", client_id: this.#config.clientId, redirect_uri: this.#config.redirectUri,
          scope: ["openid", ...SCOPES].join(" "), state: pending.state, code_challenge_method: "S256", code_challenge: challenge});
        this.#navigate(url.href);
        leaving = true;
        // The current document is leaving. The callback document's initialize
        // completes sign-in; do not signal a usable token before that exchange.
        return await abortable(new Promise(() => {}), controller.signal);
      } catch (error) {
        if (!leaving || !this.#disposed) {
          try { this.#removePending(); } catch { /* Keep the original sanitized failure. */ }
        }
        throw failure(controller.signal.aborted || error?.code === "AUTH_CANCELLED" ? "AUTH_CANCELLED" : "AUTH_FAILED");
      } finally {
        if (this.#active === controller) this.#active = null;
        this.#signing = null;
      }
    })();
    return this.#signing;
  }

  async signOut() {
    this.#generation++; this.#session = null;
    this.#active?.abort();
    this.#removePending();
    this.#initialized = true;
  }

  dispose() {
    this.#generation++; this.#session = null; this.#disposed = true;
    this.#active?.abort();
    // Do not delete the pending record on pagehide: the redirect return needs
    // it. signOut removes it; stale records are rejected after five minutes.
  }
}
