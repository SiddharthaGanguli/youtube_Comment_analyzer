import assert from "node:assert/strict";
import test from "node:test";
import {JobController} from "../job-controller.mjs";
import {AuthSession} from "../auth-session.mjs";

const NOW = Date.parse("2026-10-15T10:00:00Z");
const VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
const ID = "2a571d3b-40c4-45c2-96bc-6a89e33c9255";
const model = {release_id: "sr_" + "1".repeat(64)};
const analysis = (state = "QUEUED", update = {}) => ({analysis_id: ID, state, model,
  expires_at: new Date(NOW + 3_600_000).toISOString(), failure: null, ...update});
const result = (state = "SUCCEEDED", summary = {received: 3, analyzed: 2}) => ({state, summary, model});
const error = (code = "NETWORK_ERROR", fields = {}) => Object.assign(new Error("Safe fixture error"), {code, retryable: true, ...fields});
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  let now = NOW, hidden = false, next = 0, nextKey = 0;
  const timers = new Map(), calls = [], emissions = [];
  const auth = {signedIn: true, token: "fixture-access-token", async initialize() {}, async signIn() { this.signedIn = true; },
    async getAccessToken() { if (!this.signedIn) throw error("AUTH_REQUIRED"); return this.token; },
    async signOut() { calls.push(["signOut"]); this.signedIn = false; }, dispose() { calls.push(["dispose"]); }};
  const api = Object.fromEntries(Object.entries({submit: async () => analysis(), status: async () => analysis(),
    download: async () => result(), currentModel: async () => ({model}), delete: async () => {}})
    .map(([name, implementation]) => [name, async (...args) => { calls.push([name, ...args]); return implementation(...args); }]));
  const controller = new JobController({api, auth, onChange: value => emissions.push(value),
    now: () => now, random: () => .5, isHidden: () => hidden,
    uuid: () => `00000000-0000-4000-8000-${String(++nextKey).padStart(12, "0")}`,
    setTimer: (fn, milliseconds) => { const id = ++next; timers.set(id, {fn, due: now + milliseconds}); return id; },
    clearTimer: id => timers.delete(id)});
  return {controller, api, auth, calls, emissions, timers,
    get now() { return now; }, set hidden(value) { hidden = value; },
    setNow(value) { now = value; },
    async advance(milliseconds) {
      const target = now + milliseconds;
      for (let count = 0; count < 1000; count++) {
        const entry = [...timers.entries()].filter(([, item]) => item.due <= target).sort((a, b) => a[1].due - b[1].due)[0];
        if (!entry) { now = target; return; }
        now = entry[1].due; timers.delete(entry[0]); await entry[1].fn(); await flush();
      }
      throw new Error("Fixture timer loop did not settle");
    },
    count(name) { return calls.filter(([type]) => type === name).length; },
  };
}

test("Initialization restores a trusted session; invalid and signed-out submissions make no requests", async () => {
  const h = harness(); await h.controller.initialize();
  assert.equal(h.controller.state.phase, "ready");
  await h.controller.submit("https://evil.test/video"); assert.equal(h.count("submit"), 0);
  await h.controller.signOut(); await h.controller.submit(VIDEO);
  assert.equal(h.count("submit"), 0); assert.equal(h.controller.state.phase, "signed-out");
  h.controller.dispose();
});

test("A lost POST response retries the identical canonical body and idempotency key", async () => {
  const h = harness(); await h.controller.initialize(); let failed = true;
  h.api.submit = async (...args) => { h.calls.push(["submit", ...args]); if (failed) { failed = false; throw error(); } return analysis(); };
  await h.controller.submit("https://youtu.be/dQw4w9WgXcQ?t=10");
  assert.equal(h.controller.state.phase, "error");
  await h.controller.retry();
  const submissions = h.calls.filter(([name]) => name === "submit");
  assert.deepEqual(submissions.map(call => call.slice(1, 3)), [[VIDEO, submissions[0][2]], [VIDEO, submissions[0][2]]]);
  assert.equal(h.controller.state.phase, "QUEUED");
  h.controller.dispose();
});

test("Explicit new analysis uses a fresh key and fences an older optional model response", async () => {
  const h = harness(), details = deferred(); await h.controller.initialize();
  h.api.submit = async (...args) => { h.calls.push(["submit", ...args]); return analysis("SUCCEEDED"); };
  h.api.currentModel = async () => details.promise;
  const first = h.controller.submit(VIDEO); await flush();
  assert.equal(h.controller.state.phase, "complete");
  h.api.submit = async (...args) => { h.calls.push(["submit", ...args]); return analysis("QUEUED", {analysis_id: "new-analysis"}); };
  await h.controller.submit(VIDEO);
  details.resolve({model}); await first;
  const keys = h.calls.filter(([name]) => name === "submit").map(call => call[2]);
  assert.notEqual(keys[0], keys[1]); assert.equal(h.controller.state.analysis.analysis_id, "new-analysis");
  assert.equal(h.controller.state.result, null); assert.equal(h.controller.state.modelDetails, null);
  h.controller.dispose();
});

test("Polling follows 2/5/10 seconds and stops at terminal result with one download", async () => {
  const h = harness(); await h.controller.initialize();
  const states = ["FETCHING", "INFERENCING", "SAVING", "SUCCEEDED"];
  h.api.status = async (...args) => { h.calls.push(["status", ...args]); return analysis(states.shift()); };
  await h.controller.submit(VIDEO);
  await h.advance(1999); assert.equal(h.count("status"), 0);
  await h.advance(1); assert.equal(h.controller.state.phase, "FETCHING");
  await h.advance(5000); assert.equal(h.controller.state.phase, "INFERENCING");
  await h.advance(10000); assert.equal(h.controller.state.phase, "SAVING");
  await h.advance(10000); assert.equal(h.controller.state.phase, "complete");
  await h.advance(100000); assert.equal(h.count("status"), 4); assert.equal(h.count("download"), 1);
  await h.controller.refresh(); assert.equal(h.count("download"), 1);
  h.controller.dispose();
});

test("Partial, empty, all-skipped and failed jobs remain distinct; failed HTTP-200 jobs do not download", async () => {
  for (const [data, expected] of [[result("PARTIAL"), "partial"], [result("SUCCEEDED", {received: 0, analyzed: 0}), "empty"],
    [result("SUCCEEDED", {received: 2, analyzed: 0}), "all-skipped"]]) {
    const h = harness(); await h.controller.initialize();
    h.api.submit = async () => analysis(data.state); h.api.download = async () => data;
    await h.controller.submit(VIDEO); assert.equal(h.controller.state.phase, expected); h.controller.dispose();
  }
  const h = harness(); await h.controller.initialize();
  h.api.submit = async () => analysis("FAILED", {failure: {code: "COMMENTS_DISABLED", retryable: false}});
  await h.controller.submit(VIDEO); await h.controller.retry();
  assert.equal(h.controller.state.phase, "failed"); assert.equal(h.count("download"), 0); assert.equal(h.count("status"), 0);
  h.controller.dispose();
});

test("Quota reset and Retry-After gate automatic polls, manual retry, new submit and pause/resume", async () => {
  const h = harness(); await h.controller.initialize(); await h.controller.submit(VIDEO);
  h.api.status = async (...args) => { h.calls.push(["status", ...args]); throw error("YOUTUBE_QUOTA_EXHAUSTED", {
    retryAfterSeconds: 30, resetAt: new Date(NOW + 120000).toISOString()}); };
  await h.advance(2000); assert.equal(h.count("status"), 1);
  assert.equal(h.controller.retryAt, NOW + 120000);
  h.controller.pause(); await h.controller.resume();
  await h.controller.retry(); await h.controller.submit(VIDEO);
  await h.advance(117999); assert.equal(h.count("status"), 1); assert.equal(h.count("submit"), 1);
  h.api.status = async (...args) => { h.calls.push(["status", ...args]); return analysis(); };
  await h.advance(1); assert.equal(h.count("status"), 2);
  h.controller.dispose();
});

test("Reset-at without Retry-After still prevents early retries of a rejected submission", async () => {
  const h = harness(); await h.controller.initialize();
  h.api.submit = async (...args) => { h.calls.push(["submit", ...args]); throw error("YOUTUBE_QUOTA_EXHAUSTED", {resetAt: new Date(NOW + 60000).toISOString()}); };
  await h.controller.submit(VIDEO); await h.controller.retry();
  assert.equal(h.count("submit"), 1);
  await h.advance(60000); await h.controller.retry(); assert.equal(h.count("submit"), 2);
  h.controller.dispose();
});

test("Transient polling retries stop after five consecutive failures and reset after success", async () => {
  const h = harness(); await h.controller.initialize(); await h.controller.submit(VIDEO);
  h.api.status = async (...args) => { h.calls.push(["status", ...args]); throw error("SYSTEM_BUSY", {retryAfterSeconds: 12}); };
  await h.advance(100000); assert.equal(h.count("status"), 5); assert.equal(h.controller.state.busy, false);
  h.api.status = async (...args) => { h.calls.push(["status", ...args]); return analysis(); };
  await h.controller.retry(); assert.equal(h.controller.pollFailures, 0);
  h.api.status = async (...args) => { h.calls.push(["status", ...args]); throw error(); };
  const previous = h.count("status"); await h.advance(100000); assert.equal(h.count("status") - previous, 5);
  h.controller.dispose();
});

test("A transient error after many healthy polls still receives its bounded retry budget", async () => {
  const h = harness(); await h.controller.initialize(); await h.controller.submit(VIDEO);
  await h.advance(210000); const healthy = h.count("status"); assert.ok(healthy > 20);
  h.api.status = async (...args) => { h.calls.push(["status", ...args]); throw error(); };
  await h.advance(60000); assert.equal(h.count("status") - healthy, 5);
  h.controller.dispose();
});

test("Visibility and lifecycle changes do not silently reset exhausted transient retries", async () => {
  const h = harness(); await h.controller.initialize(); await h.controller.submit(VIDEO);
  h.api.status = async (...args) => { h.calls.push(["status", ...args]); throw error(); };
  await h.advance(100000); assert.equal(h.count("status"), 5);
  h.hidden = true; h.controller.visibilityChanged(); h.hidden = false; h.controller.visibilityChanged();
  h.controller.pause(); await h.controller.resume(); await h.advance(100000);
  assert.equal(h.count("status"), 5); assert.equal(h.controller.state.phase, "error");
  await h.controller.retry(); await h.advance(100000); assert.equal(h.count("status"), 10);
  h.controller.dispose();
});

test("Hidden documents stop polling and showing them preserves the existing backoff", async () => {
  const h = harness(); await h.controller.initialize(); await h.controller.submit(VIDEO);
  h.hidden = true; h.controller.visibilityChanged(); await h.advance(20000); assert.equal(h.count("status"), 0);
  h.hidden = false; h.controller.visibilityChanged(); await h.advance(5000); assert.equal(h.count("status"), 1);
  h.api.status = async (...args) => { h.calls.push(["status", ...args]); throw error("RATE_LIMITED", {retryAfterSeconds: 60}); };
  await h.advance(10000); const before = h.count("status");
  h.hidden = true; h.controller.visibilityChanged(); await h.advance(20000);
  h.hidden = false; h.controller.visibilityChanged(); await h.advance(39999); assert.equal(h.count("status"), before);
  await h.advance(1); assert.equal(h.count("status"), before + 1);
  h.controller.dispose();
});

test("Concurrent refreshes cannot start duplicate downloads", async () => {
  const h = harness(), pending = deferred(); await h.controller.initialize(); await h.controller.submit(VIDEO);
  let statuses = 0;
  h.api.status = async () => { statuses++; return pending.promise; };
  const first = h.controller.refresh(), second = h.controller.refresh();
  pending.resolve(analysis("SUCCEEDED")); await Promise.all([first, second]);
  assert.equal(statuses, 1); assert.equal(h.count("download"), 1);
  h.controller.dispose();
});

test("Pause cancels an unknown POST outcome and resume reuses the original key", async () => {
  const h = harness(), pending = deferred(); await h.controller.initialize();
  h.api.submit = async (...args) => { h.calls.push(["submit", ...args]); return h.count("submit") === 1 ? pending.promise : analysis(); };
  const submission = h.controller.submit(VIDEO); await flush();
  h.controller.pause(); await h.controller.resume();
  pending.resolve(analysis("SUCCEEDED")); await submission;
  assert.deepEqual(h.calls.filter(([name]) => name === "submit").map(call => call[2]), [h.controller.lastSubmission.key, h.controller.lastSubmission.key]);
  assert.equal(h.controller.state.phase, "QUEUED"); assert.equal(h.count("download"), 0);
  h.controller.dispose();
});

test("Sign-out clears results and retry metadata and fences a late POST or result", async () => {
  for (const where of ["submit", "download"]) {
    const h = harness(), pending = deferred(); await h.controller.initialize();
    h.api[where] = async () => pending.promise;
    if (where === "download") h.api.submit = async () => analysis("SUCCEEDED");
    const work = h.controller.submit(VIDEO); await flush();
    await h.controller.signOut(); pending.resolve(where === "submit" ? analysis("SUCCEEDED") : result()); await work;
    assert.equal(h.controller.state.phase, "signed-out"); assert.equal(h.controller.state.analysis, null);
    assert.equal(h.controller.state.result, null); assert.equal(h.controller.lastSubmission, null);
    assert.equal(h.count("signOut"), 1); h.controller.dispose();
  }
});

test("Deletion cancels and fences in-flight download, clearing the owned reference", async () => {
  const h = harness(), pending = deferred(); await h.controller.initialize();
  h.api.submit = async () => analysis("SUCCEEDED"); h.api.download = async () => pending.promise;
  const work = h.controller.submit(VIDEO); await flush();
  await h.controller.remove(); pending.resolve(result()); await work;
  assert.equal(h.controller.state.phase, "deleted"); assert.equal(h.controller.state.analysis, null);
  assert.equal(h.controller.state.result, null); assert.equal(h.controller.lastSubmission, null);
  h.controller.dispose();
});

test("A failed deletion keeps retained data on its original expiry timer", async () => {
  const h = harness(); await h.controller.initialize();
  h.api.submit = async () => analysis("SUCCEEDED", {expires_at: new Date(NOW + 5000).toISOString()});
  h.api.delete = async () => { throw error(); };
  await h.controller.submit(VIDEO); await h.controller.remove(); assert.ok(h.controller.state.result);
  await h.advance(5000); assert.equal(h.controller.state.phase, "expired"); assert.equal(h.controller.state.result, null);
  h.controller.dispose();
});

test("Retained data expires even while the deletion response is still pending", async () => {
  const h = harness(), deletion = deferred(); await h.controller.initialize();
  h.api.submit = async () => analysis("SUCCEEDED", {expires_at: new Date(NOW + 5000).toISOString()});
  h.api.delete = async () => deletion.promise;
  await h.controller.submit(VIDEO); const removal = h.controller.remove();
  await h.advance(5000); assert.equal(h.controller.state.phase, "expired"); assert.equal(h.controller.state.result, null);
  deletion.resolve(); await removal; assert.equal(h.controller.state.phase, "expired");
  h.controller.dispose();
});

test("Expiry aborts a pending download and its late result cannot reappear", async () => {
  const h = harness(), pending = deferred(); await h.controller.initialize();
  h.api.submit = async () => analysis("SUCCEEDED", {expires_at: new Date(NOW + 2000).toISOString()});
  h.api.download = async () => pending.promise;
  const work = h.controller.submit(VIDEO); await flush(); await h.advance(2000);
  pending.resolve(result()); await work;
  assert.equal(h.controller.state.phase, "expired"); assert.equal(h.controller.state.result, null); h.controller.dispose();
});

test("AUTH_REQUIRED from a protected request clears every retained result and retry request", async () => {
  const h = harness(); await h.controller.initialize(); await h.controller.submit(VIDEO);
  h.api.status = async () => { throw error("AUTH_REQUIRED", {retryable: false}); };
  await h.advance(2000);
  assert.equal(h.controller.state.phase, "signed-out"); assert.equal(h.controller.state.analysis, null);
  assert.equal(h.controller.lastSubmission, null); h.controller.dispose();
});

test("Session synchronization adopts a new sign-in, clears old account data and ignores stale checks", async () => {
  const h = harness(); h.auth.signedIn = false; await h.controller.initialize();
  h.auth.signedIn = true; await h.controller.syncSession(); assert.equal(h.controller.state.phase, "ready");
  h.api.submit = async () => analysis("SUCCEEDED"); await h.controller.submit(VIDEO); assert.ok(h.controller.state.result);
  h.auth.token = "different-account-access-token"; await h.controller.syncSession();
  assert.equal(h.controller.state.analysis, null); assert.equal(h.controller.state.result, null); assert.equal(h.controller.lastSubmission, null);
  const pending = deferred(); h.auth.getAccessToken = async () => pending.promise;
  const synchronization = h.controller.syncSession(); await h.controller.signOut();
  pending.resolve("old-token"); await synchronization;
  assert.equal(h.controller.state.phase, "signed-out"); h.controller.dispose();
});

test("Renderer exceptions cannot prevent secure sign-out", async () => {
  const h = harness(); await h.controller.initialize();
  h.controller.onChange = () => { throw new Error("Fixture renderer failed"); };
  await h.controller.signOut(); assert.equal(h.count("signOut"), 1);
  assert.equal(h.auth.signedIn, false); assert.equal(h.controller.state.result, null); h.controller.dispose();
});

test("Disposal clears retained private data, timers and retry metadata", async () => {
  const h = harness(); await h.controller.initialize(); h.api.submit = async () => analysis("SUCCEEDED");
  await h.controller.submit(VIDEO); assert.ok(h.controller.state.result);
  h.controller.dispose();
  assert.equal(h.controller.state.result, null); assert.equal(h.controller.state.analysis, null);
  assert.equal(h.controller.lastSubmission, null); assert.equal(h.timers.size, 0);
  const previous = h.calls.length; await h.advance(100000); await h.controller.submit(VIDEO);
  assert.equal(h.calls.length, previous);
});

test("Current-model evaluation is applied only to the result's exact release", async () => {
  const h = harness(); await h.controller.initialize(); h.api.submit = async () => analysis("SUCCEEDED");
  h.api.currentModel = async () => ({model: {release_id: "sr_" + "9".repeat(64)}});
  await h.controller.submit(VIDEO); assert.equal(h.controller.state.phase, "complete");
  assert.equal(h.controller.state.modelDetails, null); h.controller.dispose();
});

test("Actual AuthSession token-write notifications cannot cancel sign-in after local sign-out", async () => {
  const config = {enabled: true, extensionId: "a".repeat(32), clientId: "fixtureclient1",
    cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com"};
  const data = {}, listeners = new Set();
  const chromeApi = {runtime: {id: config.extensionId, getURL: () => `chrome-extension://${config.extensionId}/`},
    identity: {getRedirectURL: () => `https://${config.extensionId}.chromiumapp.org/cognito`,
      async launchWebAuthFlow({url}) { const request = new URL(url); return `${request.searchParams.get("redirect_uri")}?${new URLSearchParams({state: request.searchParams.get("state"), code: "fixture"})}`; }},
    storage: {onChanged: {addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn)}, session: {
      async setAccessLevel() {}, async get(keys) { return Object.fromEntries(keys.map(key => [key, data[key]])); },
      async set(values) { Object.assign(data, values); for (const listener of listeners) listener(Object.fromEntries(Object.entries(values).map(([key, value]) => [key, {newValue: value}])), "session"); },
      async remove(key) { const oldValue = data[key]; delete data[key]; for (const listener of listeners) listener({[key]: {oldValue}}, "session"); },
    }}};
  const encode = value => Buffer.from(JSON.stringify(value)).toString("base64url");
  const token = `${encode({alg: "RS256"})}.${encode({token_use: "access", client_id: config.clientId, exp: NOW / 1000 + 3600, scope: "analysis/read analysis/write"})}.fixture`;
  const auth = new AuthSession({config, chromeApi, now: () => NOW,
    fetchImpl: async () => new Response(JSON.stringify({access_token: token, token_type: "Bearer", expires_in: 3600}), {headers: {"Content-Type": "application/json"}})});
  const h = harness(); h.controller.auth = auth; await h.controller.initialize();
  const listener = (_, area) => { if (area === "session") void h.controller.syncSession(); }; listeners.add(listener);
  await h.controller.signIn(); assert.equal(h.controller.state.signedIn, true);
  await h.controller.signOut(); await h.controller.signIn();
  assert.equal(auth.signedIn, true); assert.equal(h.controller.state.phase, "ready"); assert.equal(h.controller.state.signedIn, true);
  listeners.delete(listener); h.controller.dispose();
});
