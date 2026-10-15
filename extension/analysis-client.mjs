import {validateClientConfig} from "./client-config.mjs";
import {validateAnalysis, validateResult, validateModelDetails} from "./result-model.mjs";

const MAX_RESULT_BYTES = 10 * 1024 * 1024;
const ERRORS = Object.freeze({
  UNAUTHORIZED: ["Sign in to continue.", false], FORBIDDEN: ["This account cannot perform that action.", false],
  NOT_FOUND: ["This analysis is unavailable.", false], EXPIRED: ["This analysis has expired or was removed.", false],
  INVALID_INPUT: ["Check the video link and comment limit.", false], REQUEST_TOO_LARGE: ["The request is too large.", false],
  IDEMPOTENCY_CONFLICT: ["This retry key belongs to a different request.", false],
  RESULT_NOT_READY: ["The analysis is still running.", true], ANALYSIS_FAILED: ["The analysis could not finish.", false],
  RATE_LIMITED: ["Please wait before trying again.", true], ACTIVE_JOB_LIMIT: ["Wait for an active analysis to finish.", true],
  SYSTEM_BUSY: ["The service is busy. Please try again shortly.", true],
  YOUTUBE_QUOTA_EXHAUSTED: ["The daily YouTube allowance is exhausted.", true],
  LIVE_ANALYSIS_DISABLED: ["Live analysis is not available yet.", false],
  DEPENDENCY_UNAVAILABLE: ["A required service is temporarily unavailable.", true],
  INVALID_RESPONSE: ["The service response could not be verified.", false],
  NETWORK_ERROR: ["The request could not reach the service.", true], REQUEST_TIMEOUT: ["The request timed out.", true],
  REQUEST_ABORTED: ["The request was cancelled.", false], DOWNLOAD_FAILED: ["The analysis result could not be downloaded.", true],
  CONFIGURATION_REQUIRED: ["The extension connection has not been configured.", false],
});
const HTTP_CODES = {UNAUTHORIZED: 401, FORBIDDEN: 403, NOT_FOUND: 404, EXPIRED: 410, INVALID_INPUT: 422,
  REQUEST_TOO_LARGE: 413, IDEMPOTENCY_CONFLICT: 409, RESULT_NOT_READY: 409, ANALYSIS_FAILED: 409,
  RATE_LIMITED: 429, ACTIVE_JOB_LIMIT: 429, SYSTEM_BUSY: 503, YOUTUBE_QUOTA_EXHAUSTED: 503,
  LIVE_ANALYSIS_DISABLED: 503, DEPENDENCY_UNAVAILABLE: 503};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export class ApiError extends Error {
  constructor(code, {retryAfterSeconds = null, resetAt = null} = {}) {
    const safe = Object.hasOwn(ERRORS, code) ? code : "DEPENDENCY_UNAVAILABLE";
    super(ERRORS[safe][0]); this.name = "ApiError"; this.code = safe;
    this.retryable = ERRORS[safe][1];
    this.retryAfterSeconds = Number.isSafeInteger(retryAfterSeconds) && retryAfterSeconds > 0
      ? Math.min(172800, retryAfterSeconds) : null;
    this.resetAt = validTimestamp(resetAt) ? resetAt : null;
  }
}

function validTimestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) return false;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 19) === value.slice(0, 19);
}

function checked(validate, value, expected) {
  try { return validate(value, expected); } catch { throw new ApiError("INVALID_RESPONSE"); }
}

function json(bytes) {
  try { return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes)); }
  catch { throw new ApiError("INVALID_RESPONSE"); }
}

function identifier(value) {
  if (typeof value !== "string" || !UUID.test(value)) throw new ApiError("INVALID_INPUT");
  return value.toLowerCase();
}

function canonicalVideo(value) {
  if (typeof value !== "string" || value.length > 1024 || /[\s\x00-\x1f\x7f]/.test(value)) throw new ApiError("INVALID_INPUT");
  let url;
  try { url = new URL(value); } catch { throw new ApiError("INVALID_INPUT"); }
  if (url.protocol !== "https:" || url.username || url.password || url.port) throw new ApiError("INVALID_INPUT");
  let id;
  if (url.hostname === "youtu.be") id = /^\/([A-Za-z0-9_-]{11})\/?$/.exec(url.pathname)?.[1];
  else if (["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"].includes(url.hostname)) {
    if (url.pathname === "/watch" && url.searchParams.getAll("v").length === 1) id = url.searchParams.get("v");
    else id = /^\/(?:shorts|live)\/([A-Za-z0-9_-]{11})\/?$/.exec(url.pathname)?.[1];
  }
  if (!/^[A-Za-z0-9_-]{11}$/.test(id ?? "")) throw new ApiError("INVALID_INPUT");
  return `https://www.youtube.com/watch?v=${id}`;
}

function retryAfter(headers, fallback) {
  const value = headers.get("Retry-After");
  return value && /^[0-9]{1,8}$/.test(value) ? Math.min(172800, Math.max(1, Number(value)))
    : Number.isSafeInteger(fallback) && fallback > 0 ? Math.min(172800, fallback) : null;
}

function apiFailure(response) {
  let envelope;
  try { envelope = json(response.bytes)?.error; } catch { /* Native gateway errors have another shape. */ }
  const fallback = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 410: "EXPIRED", 413: "REQUEST_TOO_LARGE",
    422: "INVALID_INPUT", 429: "RATE_LIMITED"}[response.status] ?? (response.status >= 500 ? "DEPENDENCY_UNAVAILABLE" : "INVALID_RESPONSE");
  const code = envelope && typeof envelope === "object" && Object.hasOwn(HTTP_CODES, envelope.code)
    && HTTP_CODES[envelope.code] === response.status ? envelope.code : fallback;
  return new ApiError(code, {retryAfterSeconds: retryAfter(response.headers, envelope?.retry_after_seconds),
    resetAt: code === "YOUTUBE_QUOTA_EXHAUSTED" ? envelope?.reset_at : null});
}

export class AnalysisClient {
  constructor({config, auth, fetchImpl = globalThis.fetch, now = Date.now}) {
    this.config = validateClientConfig(config);
    if (!this.config.enabled) throw new ApiError("CONFIGURATION_REQUIRED");
    if (!auth || typeof auth.getAccessToken !== "function" || typeof fetchImpl !== "function" || typeof now !== "function") {
      throw new ApiError("CONFIGURATION_REQUIRED");
    }
    this.auth = auth; this.fetchImpl = fetchImpl; this.now = now;
  }

  async _fetch(url, options, {signal, maximum = 65536, timeout = 10000, skipErrorBody = false} = {}) {
    if (signal?.aborted) throw new ApiError("REQUEST_ABORTED");
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal?.addEventListener("abort", cancel, {once: true});
    let timedOut = false;
    const aborted = new Promise((_, reject) => controller.signal.addEventListener("abort", () =>
      reject(new ApiError(timedOut ? "REQUEST_TIMEOUT" : "REQUEST_ABORTED")), {once: true}));
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    try {
      const response = await Promise.race([this.fetchImpl(url, {...options, signal: controller.signal,
        credentials: "omit", redirect: "error", cache: "no-store", referrerPolicy: "no-referrer"}), aborted]);
      if (!response || !Number.isInteger(response.status) || response.status < 100 || response.status > 599
          || !response.headers?.get || response.redirected) {
        throw new ApiError("INVALID_RESPONSE");
      }
      if (response.url && response.url !== url) throw new ApiError("INVALID_RESPONSE");
      if (skipErrorBody && response.status !== 200) {
        if (response.body) void response.body.cancel().catch(() => {});
        return {status: response.status, headers: response.headers, bytes: new Uint8Array()};
      }
      const declared = response.headers.get("Content-Length");
      if (declared !== null && (!/^[0-9]{1,12}$/.test(declared) || Number(declared) > maximum)) throw new ApiError("INVALID_RESPONSE");
      const chunks = []; let size = 0;
      if (response.body) {
        const reader = response.body.getReader();
        try {
          while (true) {
            const {done, value} = await Promise.race([reader.read(), aborted]);
            if (controller.signal.aborted) throw new ApiError(timedOut ? "REQUEST_TIMEOUT" : "REQUEST_ABORTED");
            if (done) break;
            if (!(value instanceof Uint8Array)) throw new ApiError("INVALID_RESPONSE");
            size += value.byteLength;
            if (size > maximum) throw new ApiError("INVALID_RESPONSE");
            chunks.push(value);
          }
        } finally { void reader.cancel().catch(() => {}); reader.releaseLock(); }
      }
      const bytes = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      return {status: response.status, headers: response.headers, bytes};
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(timedOut ? "REQUEST_TIMEOUT" : signal?.aborted ? "REQUEST_ABORTED" : "NETWORK_ERROR");
    } finally { clearTimeout(timer); signal?.removeEventListener("abort", cancel); }
  }

  async _api(path, {method = "GET", body, idempotencyKey, signal} = {}) {
    if (signal?.aborted) throw new ApiError("REQUEST_ABORTED");
    let token;
    try { token = await this.auth.getAccessToken(); } catch { throw new ApiError("UNAUTHORIZED"); }
    if (typeof token !== "string" || !/^[A-Za-z0-9._~-]{1,16384}$/.test(token)) throw new ApiError("UNAUTHORIZED");
    const headers = {Authorization: `Bearer ${token}`, Accept: "application/json"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    const response = await this._fetch(this.config.apiBaseUrl + path, {method, headers, body}, {signal});
    if (response.status < 200 || response.status >= 300) throw apiFailure(response);
    if (response.status !== 204 && response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
      throw new ApiError("INVALID_RESPONSE");
    }
    return {response, data: response.status === 204 ? null : json(response.bytes)};
  }

  async submit(videoUrl, idempotencyKey, {signal} = {}) {
    const key = identifier(idempotencyKey), video_url = canonicalVideo(videoUrl);
    const reply = await this._api("/v1/analyses", {method: "POST", body: JSON.stringify({video_url,
      max_comments: this.config.maxComments}), idempotencyKey: key, signal});
    const analysis = checked(validateAnalysis, reply.data);
    const terminal = ["SUCCEEDED", "PARTIAL", "FAILED"].includes(analysis.state);
    if (![200, 202].includes(reply.response.status) || terminal !== (reply.response.status === 200)
        || analysis.video_id !== video_url.split("=")[1]
        || analysis.sampling.requested_max_comments !== this.config.maxComments) throw new ApiError("INVALID_RESPONSE");
    return analysis;
  }

  async status(id, {signal} = {}) {
    id = identifier(id);
    const reply = await this._api(`/v1/analyses/${id}`, {signal});
    const analysis = checked(validateAnalysis, reply.data);
    if (reply.response.status !== 200 || analysis.analysis_id.toLowerCase() !== id) throw new ApiError("INVALID_RESPONSE");
    return analysis;
  }

  async delete(id, {signal} = {}) {
    const reply = await this._api(`/v1/analyses/${identifier(id)}`, {method: "DELETE", signal});
    if (reply.response.status !== 204 || reply.response.bytes.length) throw new ApiError("INVALID_RESPONSE");
  }

  async currentModel({signal} = {}) {
    const reply = await this._api("/v1/models/current", {signal});
    if (reply.response.status !== 200) throw new ApiError("INVALID_RESPONSE");
    return checked(validateModelDetails, reply.data);
  }

  _descriptor(raw) {
    const keys = ["url", "expires_at", "sha256", "bytes", "content_type", "schema_version"];
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || Object.keys(raw).length !== keys.length
        || keys.some(key => !Object.hasOwn(raw, key)) || typeof raw.url !== "string" || raw.url.length > 16384
        || /[\s\x00-\x1f\x7f]/.test(raw.url) || raw.url.includes("\\") || !validTimestamp(raw.expires_at)
        || typeof raw.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(raw.sha256)
        || !Number.isSafeInteger(raw.bytes) || raw.bytes < 1 || raw.bytes > MAX_RESULT_BYTES
        || raw.content_type !== "application/json" || raw.schema_version !== 1) throw new ApiError("INVALID_RESPONSE");
    let url;
    try { url = new URL(raw.url); } catch { throw new ApiError("INVALID_RESPONSE"); }
    const base = new URL(this.config.resultBaseUrl), prefix = base.pathname + "/";
    const relative = url.pathname.slice(prefix.length);
    if (url.origin !== base.origin || url.username || url.password || url.hash || url.port
        || !url.pathname.startsWith(prefix) || raw.url.includes("%") && url.pathname.includes("%")
        || !/^work\/[0-9a-f-]{36}\/attempt\/[1-9][0-9]*\/results\/[a-f0-9]{64}\.json$/.test(relative)
        || !UUID.test(relative.split("/")[1]) || !relative.endsWith(`/results/${raw.sha256}.json`)
        || Date.parse(raw.expires_at) > this.now() + 301000) throw new ApiError("INVALID_RESPONSE");
    return raw;
  }

  async download(id, {signal, analysis} = {}) {
    id = identifier(id);
    const expected = checked(validateAnalysis, analysis ?? await this.status(id, {signal}));
    if (expected.analysis_id.toLowerCase() !== id) throw new ApiError("INVALID_RESPONSE");
    if (!expected.result_available) throw new ApiError(expected.state === "FAILED" ? "ANALYSIS_FAILED" : "RESULT_NOT_READY");
    if (Date.parse(expected.expires_at) <= this.now()) throw new ApiError("EXPIRED");
    let first;
    for (let attempt = 0; attempt < 2; attempt++) {
      const reply = await this._api(`/v1/analyses/${id}/result`, {signal});
      if (reply.response.status !== 200) throw new ApiError("INVALID_RESPONSE");
      const descriptor = this._descriptor(reply.data);
      if (Date.parse(descriptor.expires_at) > Date.parse(expected.expires_at)) throw new ApiError("INVALID_RESPONSE");
      if (first && (first.sha256 !== descriptor.sha256 || first.bytes !== descriptor.bytes
          || new URL(first.url).pathname !== new URL(descriptor.url).pathname)) throw new ApiError("INVALID_RESPONSE");
      first = descriptor;
      if (Date.parse(descriptor.expires_at) <= this.now()) {
        if (attempt === 0) continue;
        throw new ApiError("EXPIRED");
      }
      // This request deliberately has no Authorization header. The signed URL
      // is scoped to the configured private result prefix, never another host.
      const object = await this._fetch(descriptor.url, {method: "GET", headers: {Accept: "application/json"}},
        {signal, maximum: descriptor.bytes, timeout: 20000, skipErrorBody: true});
      if ([401, 403].includes(object.status) && attempt === 0) continue;
      if (object.status !== 200) throw new ApiError("DOWNLOAD_FAILED");
      if (object.bytes.byteLength !== descriptor.bytes) throw new ApiError("INVALID_RESPONSE");
      const digest = await globalThis.crypto.subtle.digest("SHA-256", object.bytes);
      const sha256 = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
      if (sha256 !== descriptor.sha256) throw new ApiError("INVALID_RESPONSE");
      const result = checked(validateResult, json(object.bytes), expected);
      if (Math.min(Date.parse(expected.expires_at), Date.parse(result.expires_at)) <= this.now()) throw new ApiError("EXPIRED");
      return result;
    }
    throw new ApiError("DOWNLOAD_FAILED");
  }
}
