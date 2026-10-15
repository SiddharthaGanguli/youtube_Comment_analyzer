import {createAuthorizationRequest, consumeAuthorizationCallback} from "./auth-pkce.mjs";

const SKEW_MS = 30_000;
const MAX_TOKEN_BYTES = 16_384;
const MAX_RESPONSE_BYTES = 65_536;
const MAX_LIFETIME_SECONDS = 86_400;
const TOKEN_TIMEOUT_MS = 10_000;
const SCOPES = ["analysis/read", "analysis/write"];

function failure(code = "AUTH_FAILED") {
  const error = new Error(code === "AUTH_REQUIRED" ? "Sign in to continue." :
    code === "AUTH_CANCELLED" ? "Sign-in was cancelled." :
    code === "AUTH_UNAVAILABLE" ? "Secure extension sign-in is unavailable." : "Sign-in could not be completed. Please try again.");
  error.code = code;
  return error;
}

function cancelled(signal) {
  if (signal.aborted) throw failure("AUTH_CANCELLED");
}

function abortable(promise, signal) {
  if (signal.aborted) return Promise.reject(failure("AUTH_CANCELLED"));
  return new Promise((resolve, reject) => {
    const abort = () => reject(failure("AUTH_CANCELLED"));
    signal.addEventListener("abort", abort, {once: true});
    Promise.resolve(promise).then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}

function claims(token) {
  if (typeof token !== "string" || token.length > MAX_TOKEN_BYTES || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) throw failure();
  try {
    const encoded = token.split(".")[1];
    const bytes = Uint8Array.from(atob(encoded.replaceAll("-", "+").replaceAll("_", "/")), character => character.charCodeAt(0));
    return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));
  } catch { throw failure(); }
}

function requiredScopes(value) {
  return typeof value === "string" && value.length <= 2048 && SCOPES.every(scope => value.split(" ").includes(scope));
}

async function readTokenResponse(response, signal) {
  if (!response?.ok || response.redirected || response.status !== 200) throw failure();
  const length = response.headers?.get("Content-Length");
  if (length !== null && length !== undefined && (!/^\d{1,8}$/.test(length) || Number(length) > MAX_RESPONSE_BYTES)) throw failure();
  const type = response.headers?.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase();
  if (type !== "application/json" || !response.body?.getReader) throw failure();
  const reader = response.body.getReader(), chunks = [];
  let size = 0;
  try {
    while (true) {
      const {value, done} = await abortable(reader.read(), signal);
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
    // Cancel rather than drain an oversized or aborted response. No response
    // body, OAuth code, verifier or token is sent to logs or error messages.
    try { reader.cancel().catch(() => {}); } catch { /* Closing a failed stream is best effort. */ }
    try { reader.releaseLock(); } catch { /* An aborted read may still be settling. */ }
  }
}

export class AuthSession {
  #config; #chrome; #fetch; #now; #key; #revisionKey;
  #session = null; #revision = ""; #initialized = false; #initializing = null;
  #signing = null; #active = null; #pending = null; #generation = 0; #disposed = false; #signedOut = false;
  #listener;

  constructor({config, chromeApi = globalThis.chrome, fetchImpl = globalThis.fetch?.bind(globalThis), now = Date.now}) {
    if (!config || config.enabled !== true || !/^[a-p]{32}$/.test(config.extensionId)
        || !/^[a-z0-9]{1,128}$/.test(config.clientId) || typeof fetchImpl !== "function" || typeof now !== "function") throw failure("AUTH_UNAVAILABLE");
    this.#config = {...config};
    this.#chrome = chromeApi; this.#fetch = fetchImpl; this.#now = now;
    this.#key = `commentLens.auth.${config.extensionId}.${config.clientId}`;
    this.#revisionKey = `${this.#key}.signOutRevision`;
    this.#listener = (changes, area) => {
      if (area !== "session" || this.#disposed) return;
      if (Object.hasOwn(changes, this.#revisionKey)) {
        this.#revision = this.#checkedRevision(changes[this.#revisionKey].newValue);
        this.#invalidate();
      }
      if (Object.hasOwn(changes, this.#key)) {
        this.#session = this.#checkedSession(changes[this.#key].newValue);
        if (!this.#session && changes[this.#key].oldValue) this.#invalidate();
      }
    };
  }

  #clock() {
    const now = this.#now();
    if (!Number.isSafeInteger(now) || now < 0) throw failure("AUTH_UNAVAILABLE");
    return now;
  }

  #checkedRevision(value) { return typeof value === "string" && /^[a-f0-9-]{36}$/.test(value) ? value : ""; }

  #checkedSession(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)
        || Object.keys(value).sort().join(",") !== "accessToken,expiresAtMs,revision"
        || typeof value.accessToken !== "string" || value.accessToken.length > MAX_TOKEN_BYTES
        || !Number.isSafeInteger(value.expiresAtMs) || value.expiresAtMs <= this.#clock() + SKEW_MS
        || value.expiresAtMs > this.#clock() + MAX_LIFETIME_SECONDS * 1000
        || value.revision !== this.#revision) return null;
    try {
      const payload = claims(value.accessToken);
      if (payload.token_use !== "access" || payload.client_id !== this.#config.clientId || !requiredScopes(payload.scope)
          || !Number.isSafeInteger(payload.exp) || value.expiresAtMs > payload.exp * 1000) return null;
      return {accessToken: value.accessToken, expiresAtMs: value.expiresAtMs, revision: value.revision};
    } catch { return null; }
  }

  #invalidate() {
    this.#generation++; this.#session = null;
    this.#active?.abort();
    if (this.#pending) { this.#pending.verifier = ""; this.#pending.consumed = true; }
    this.#pending = null;
  }

  #assertRuntime() {
    const api = this.#chrome, expected = `chrome-extension://${this.#config.extensionId}/`;
    if (this.#disposed || api?.runtime?.id !== this.#config.extensionId || api.runtime.getURL?.("") !== expected
        || api.identity?.getRedirectURL?.("cognito") !== `https://${this.#config.extensionId}.chromiumapp.org/cognito`
        || typeof api.identity.launchWebAuthFlow !== "function" || !api.storage?.session?.setAccessLevel
        || !api.storage.session.get || !api.storage.session.set || !api.storage.session.remove
        || !api.storage.onChanged?.addListener || !api.storage.onChanged?.removeListener) throw failure("AUTH_UNAVAILABLE");
  }

  get signedIn() { return !this.#disposed && !this.#signedOut && this.#initialized && !!this.#session && this.#session.expiresAtMs > this.#clock() + SKEW_MS; }

  async initialize() {
    this.#assertRuntime();
    if (this.#initialized) return this.signedIn;
    if (this.#initializing) return this.#initializing;
    this.#initializing = (async () => {
      try {
        await this.#chrome.storage.session.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"});
        this.#assertRuntime();
        this.#chrome.storage.onChanged.addListener(this.#listener);
        const generation = this.#generation;
        const data = await this.#chrome.storage.session.get([this.#key, this.#revisionKey]);
        if (generation === this.#generation && !this.#disposed) {
          this.#revision = this.#checkedRevision(data[this.#revisionKey]);
          this.#session = this.#checkedSession(data[this.#key]);
        }
        this.#initialized = true;
        return this.signedIn;
      } catch {
        this.#chrome?.storage?.onChanged?.removeListener?.(this.#listener);
        this.#invalidate();
        throw failure("AUTH_UNAVAILABLE");
      } finally { this.#initializing = null; }
    })();
    return this.#initializing;
  }

  async getAccessToken() {
    await this.initialize();
    if (this.#signedOut) throw failure("AUTH_REQUIRED");
    try {
      const generation = this.#generation;
      const data = await this.#chrome.storage.session.get([this.#key, this.#revisionKey]);
      if (this.#disposed || generation !== this.#generation) throw failure("AUTH_REQUIRED");
      this.#revision = this.#checkedRevision(data[this.#revisionKey]);
      this.#session = this.#checkedSession(data[this.#key]);
      if (!this.signedIn) throw failure("AUTH_REQUIRED");
      return this.#session.accessToken;
    } catch { this.#session = null; throw failure("AUTH_REQUIRED"); }
  }

  signIn() {
    if (this.#signing) return this.#signing;
    const generation = this.#generation, controller = new AbortController();
    this.#active = controller;
    const signal = controller.signal;
    this.#signing = (async () => {
      let timer;
      try {
        await abortable(this.initialize(), signal);
        const snapshot = await abortable(this.#chrome.storage.session.get([this.#revisionKey]), signal);
        const revision = this.#checkedRevision(snapshot[this.#revisionKey]);
        if (generation !== this.#generation) throw failure("AUTH_CANCELLED");
        const request = await abortable(createAuthorizationRequest(this.#config, this.#clock()), signal);
        this.#pending = request.pending;
        const callback = await abortable(this.#chrome.identity.launchWebAuthFlow({url: request.url, interactive: true}), signal);
        cancelled(signal);
        const exchange = consumeAuthorizationCallback(callback, request.pending, this.#clock());
        timer = setTimeout(() => controller.abort(), TOKEN_TIMEOUT_MS);
        const response = await abortable(this.#fetch(exchange.endpoint, {...exchange.options, signal}), signal);
        if (response.url && response.url !== exchange.endpoint) throw failure();
        const data = await readTokenResponse(response, signal);
        clearTimeout(timer); timer = undefined;
        if (typeof data.token_type !== "string" || data.token_type.toLowerCase() !== "bearer"
            || !Number.isSafeInteger(data.expires_in) || data.expires_in <= SKEW_MS / 1000 || data.expires_in > MAX_LIFETIME_SECONDS
            || data.scope !== undefined && !requiredScopes(data.scope)) throw failure();
        // These decoded claims bound client behavior only. API Gateway performs
        // signature validation and authorizes every protected backend request.
        const payload = claims(data.access_token), now = this.#clock();
        if (payload.token_use !== "access" || payload.client_id !== this.#config.clientId || !requiredScopes(payload.scope)
            || !Number.isSafeInteger(payload.exp) || payload.exp * 1000 <= now + SKEW_MS) throw failure();
        const session = {accessToken: data.access_token, expiresAtMs: Math.min(now + data.expires_in * 1000, payload.exp * 1000), revision};
        const latest = await abortable(this.#chrome.storage.session.get([this.#revisionKey]), signal);
        if (generation !== this.#generation || revision !== this.#checkedRevision(latest[this.#revisionKey])) throw failure("AUTH_CANCELLED");
        await this.#chrome.storage.session.set({[this.#key]: session});
        const saved = await this.#chrome.storage.session.get([this.#key, this.#revisionKey]);
        cancelled(signal);
        if (generation !== this.#generation || revision !== this.#checkedRevision(saved[this.#revisionKey])) throw failure("AUTH_CANCELLED");
        this.#revision = revision;
        this.#session = this.#checkedSession(saved[this.#key]);
        if (!this.#session) throw failure();
        this.#signedOut = false;
        return true;
      } catch (error) {
        throw failure(signal.aborted || error?.code === "AUTH_CANCELLED" ? "AUTH_CANCELLED" : "AUTH_FAILED");
      } finally {
        clearTimeout(timer);
        if (this.#pending) { this.#pending.verifier = ""; this.#pending.consumed = true; }
        this.#pending = null; this.#active = null; this.#signing = null;
      }
    })();
    return this.#signing;
  }

  async signOut() {
    this.#signedOut = true;
    this.#invalidate();
    await this.initialize();
    try {
      // Changing a separate nonsecret revision also cancels another window's
      // pending sign-in when no access-token record has been written yet.
      await this.#chrome.storage.session.set({[this.#revisionKey]: globalThis.crypto.randomUUID()});
      await this.#chrome.storage.session.remove(this.#key);
    } catch { throw failure("AUTH_UNAVAILABLE"); }
  }

  dispose() {
    this.#invalidate(); this.#disposed = true;
    this.#chrome?.storage?.onChanged?.removeListener?.(this.#listener);
  }
}
