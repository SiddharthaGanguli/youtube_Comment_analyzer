import assert from "node:assert/strict";
import test from "node:test";
import {AuthSession} from "../auth-session.mjs";

const config = {enabled: true, extensionId: "a".repeat(32), clientId: "fixtureclient1",
  cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com"};
const NOW = 1_792_022_400_000;
const PRIVATE = "synthetic-private-details-never-in-errors";

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function jwt(update = {}) {
  const encode = value => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${encode({alg: "RS256"})}.${encode({token_use: "access", client_id: config.clientId,
    scope: "openid analysis/read analysis/write", exp: NOW / 1000 + 3600, ...update})}.fixture-signature`;
}

function tokenResponse(update = {}, options = {}) {
  return new Response(JSON.stringify({access_token: jwt(), token_type: "Bearer", expires_in: 3600,
    id_token: "discard-this-id-token", refresh_token: "discard-this-refresh-token", ...update}),
    {status: 200, headers: {"Content-Type": "application/json"}, ...options});
}

function chromeFixture() {
  const data = {unrelatedDashboardPreference: "preserve"}, listeners = new Set(), calls = [];
  let trusted = false;
  const changed = values => {
    for (const listener of listeners) listener(structuredClone(values), "session");
  };
  const fixture = {data, calls, listeners,
    runtime: {id: config.extensionId, getURL: path => `chrome-extension://${config.extensionId}/${path}`},
    identity: {getRedirectURL: path => `https://${config.extensionId}.chromiumapp.org/${path}`,
      async launchWebAuthFlow(options) {
        calls.push(["launch", options]);
        const request = new URL(options.url);
        return `${request.searchParams.get("redirect_uri")}?${new URLSearchParams({state: request.searchParams.get("state"), code: "fixture-code"})}`;
      }},
    storage: {onChanged: {addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn)},
      session: {
        async setAccessLevel(options) { calls.push(["access", options]); assert.equal(options.accessLevel, "TRUSTED_CONTEXTS"); trusted = true; },
        async get(keys) { assert.ok(trusted); calls.push(["get", keys]); return Object.fromEntries(keys.map(key => [key, structuredClone(data[key])])); },
        async set(values) {
          assert.ok(trusted); calls.push(["set", Object.keys(values)]);
          const updates = {};
          for (const [key, value] of Object.entries(values)) { updates[key] = {oldValue: data[key], newValue: structuredClone(value)}; data[key] = structuredClone(value); }
          changed(updates);
        },
        async remove(key) {
          assert.ok(trusted); calls.push(["remove", key]);
          if (Object.hasOwn(data, key)) { const oldValue = data[key]; delete data[key]; changed({[key]: {oldValue}}); }
        },
        clear() { throw new Error("Global storage clearing is forbidden"); },
      },
      local: new Proxy({}, {get() { throw new Error("Local token persistence is forbidden"); }}),
      sync: new Proxy({}, {get() { throw new Error("Synced token persistence is forbidden"); }}),
    }};
  return fixture;
}

function setup({chrome = chromeFixture(), fetchImpl = async () => tokenResponse(), now = () => NOW} = {}) {
  const auth = new AuthSession({config, chromeApi: chrome, fetchImpl, now});
  return {auth, chrome};
}

async function until(predicate) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  throw new Error("Fixture operation did not reach expected point");
}

test("Initialization restricts session storage before reading and never opens sign-in", async () => {
  const {auth, chrome} = setup();
  assert.equal(await auth.initialize(), false);
  assert.equal(auth.signedIn, false);
  assert.equal(chrome.calls[0][0], "access");
  assert.equal(chrome.calls.filter(([type]) => type === "launch").length, 0);
  await assert.rejects(auth.getAccessToken(), {code: "AUTH_REQUIRED"});
  auth.dispose();
});

test("User sign-in exchanges one PKCE code and stores only a bounded access-token session", async () => {
  const exchanges = [];
  const {auth, chrome} = setup({fetchImpl: async (url, options) => { exchanges.push([url, options]); return tokenResponse(); }});
  assert.equal(await auth.signIn(), true);
  assert.equal(auth.signedIn, true);
  assert.equal(await auth.getAccessToken(), jwt());
  const [url, options] = exchanges[0];
  assert.equal(url, `${config.cognitoDomain}/oauth2/token`);
  assert.equal(options.method, "POST"); assert.equal(options.credentials, "omit");
  assert.equal(options.redirect, "error"); assert.equal(options.cache, "no-store");
  assert.ok(options.signal instanceof AbortSignal);
  const body = new URLSearchParams(options.body);
  assert.equal(body.get("grant_type"), "authorization_code");
  assert.equal(body.get("client_id"), config.clientId);
  assert.match(body.get("code_verifier"), /^[A-Za-z0-9_-]{43}$/);
  assert.equal(chrome.calls.find(([type]) => type === "launch")[1].interactive, true);
  const saved = Object.values(chrome.data).find(value => value && typeof value === "object" && value.accessToken);
  assert.deepEqual(Object.keys(saved).sort(), ["accessToken", "expiresAtMs", "revision"]);
  assert.equal(saved.expiresAtMs, NOW + 3_600_000);
  for (const value of ["discard-this-id-token", "discard-this-refresh-token", body.get("code_verifier")]) assert.ok(!JSON.stringify(chrome.data).includes(value));
  auth.dispose();
});

test("Restart reuses a valid access token, expiry uses skew and requires another user sign-in", async () => {
  const chrome = chromeFixture();
  let now = NOW;
  const first = setup({chrome, now: () => now}).auth;
  await first.signIn(); first.dispose();
  const second = setup({chrome, now: () => now}).auth;
  assert.equal(await second.initialize(), true);
  assert.equal(await second.getAccessToken(), jwt());
  now += 3_570_000;
  assert.equal(second.signedIn, false);
  await assert.rejects(second.getAccessToken(), {code: "AUTH_REQUIRED"});
  assert.equal(chrome.calls.filter(([type]) => type === "launch").length, 1);
  second.dispose();
  const expired = setup({chrome, now: () => now}).auth;
  assert.equal(await expired.initialize(), false);
  expired.dispose();
});

test("Wrong extension runtime, callback registration and disabled settings fail before token exchange", async () => {
  for (const mutation of [chrome => { chrome.runtime.id = "b".repeat(32); },
    chrome => { chrome.runtime.getURL = () => "https://hosted-demo.example/"; },
    chrome => { chrome.identity.getRedirectURL = () => "https://evil.example/cognito"; }]) {
    const chrome = chromeFixture(); mutation(chrome);
    let fetched = false;
    const {auth} = setup({chrome, fetchImpl: async () => { fetched = true; return tokenResponse(); }});
    await assert.rejects(auth.initialize(), {code: "AUTH_UNAVAILABLE"});
    assert.equal(fetched, false); assert.equal(chrome.calls.length, 0); auth.dispose();
  }
  assert.throws(() => new AuthSession({config: {...config, enabled: false}, chromeApi: chromeFixture()}), {code: "AUTH_UNAVAILABLE"});
});

test("Mismatched state, callback origin and OAuth errors never exchange or expose private details", async () => {
  for (const result of [request => `${request.searchParams.get("redirect_uri")}?state=wrong&code=${PRIVATE}`,
    () => `https://evil.example/cognito?state=${PRIVATE}`,
    request => `${request.searchParams.get("redirect_uri")}?${new URLSearchParams({state: request.searchParams.get("state"), error: "access_denied", error_description: PRIVATE})}`]) {
    const chrome = chromeFixture(); let fetched = false;
    chrome.identity.launchWebAuthFlow = async ({url}) => result(new URL(url));
    const {auth} = setup({chrome, fetchImpl: async () => { fetched = true; return tokenResponse(); }});
    await assert.rejects(auth.signIn(), error => error.code === "AUTH_FAILED" && !error.message.includes(PRIVATE));
    assert.equal(fetched, false); assert.equal(auth.signedIn, false);
    assert.ok(!JSON.stringify(chrome.data).includes(PRIVATE)); auth.dispose();
  }
});

test("Storage access failure stays closed without reading data or launching OAuth", async () => {
  const chrome = chromeFixture();
  chrome.storage.session.setAccessLevel = async () => { throw new Error(PRIVATE); };
  const {auth} = setup({chrome});
  await assert.rejects(auth.initialize(), error => error.code === "AUTH_UNAVAILABLE" && !error.message.includes(PRIVATE));
  assert.equal(chrome.calls.length, 0); assert.equal(auth.signedIn, false); auth.dispose();
});

test("Storage write failures cannot report a successful sign-in or restore local sign-out", async () => {
  const chrome = chromeFixture(), normalSet = chrome.storage.session.set;
  chrome.storage.session.set = async () => { throw new Error(PRIVATE); };
  const {auth} = setup({chrome});
  await assert.rejects(auth.signIn(), error => error.code === "AUTH_FAILED" && !error.message.includes(PRIVATE));
  assert.equal(auth.signedIn, false);
  chrome.storage.session.set = normalSet;
  await auth.signIn();
  chrome.storage.session.set = async () => { throw new Error(PRIVATE); };
  await assert.rejects(auth.signOut(), error => error.code === "AUTH_UNAVAILABLE" && !error.message.includes(PRIVATE));
  assert.equal(auth.signedIn, false);
  await assert.rejects(auth.getAccessToken(), {code: "AUTH_REQUIRED"});
  assert.equal(chrome.data.unrelatedDashboardPreference, "preserve"); auth.dispose();
});

test("A callback older than five minutes cannot be exchanged", async () => {
  let now = NOW, fetched = false;
  const chrome = chromeFixture();
  chrome.identity.launchWebAuthFlow = async ({url}) => {
    const request = new URL(url); now += 300_001;
    return `${request.searchParams.get("redirect_uri")}?${new URLSearchParams({state: request.searchParams.get("state"), code: "late-code"})}`;
  };
  const {auth} = setup({chrome, now: () => now, fetchImpl: async () => { fetched = true; return tokenResponse(); }});
  await assert.rejects(auth.signIn(), {code: "AUTH_FAILED"});
  assert.equal(fetched, false); auth.dispose();
});

test("Concurrent sign-in calls share one user flow; cancellation leaves no secret persistence", async () => {
  const chrome = chromeFixture(), flow = deferred();
  let launches = 0;
  chrome.identity.launchWebAuthFlow = () => { launches++; return flow.promise; };
  const {auth} = setup({chrome});
  const first = auth.signIn(), second = auth.signIn();
  assert.equal(first, second);
  const failed = assert.rejects(first, error => !error.message.includes(PRIVATE));
  await until(() => launches === 1);
  flow.reject(new Error(PRIVATE)); await failed;
  assert.equal(launches, 1); assert.equal(auth.signedIn, false);
  assert.deepEqual(chrome.data, {unrelatedDashboardPreference: "preserve"}); auth.dispose();
});

test("Sign-out cancels an in-flight identity callback before it can exchange tokens", async () => {
  const chrome = chromeFixture(), flow = deferred();
  let launched, fetched = false;
  chrome.identity.launchWebAuthFlow = options => { launched = options; return flow.promise; };
  const {auth} = setup({chrome, fetchImpl: async () => { fetched = true; return tokenResponse(); }});
  const pending = auth.signIn(), rejected = assert.rejects(pending, {code: "AUTH_CANCELLED"});
  await until(() => launched);
  await auth.signOut(); await rejected;
  const request = new URL(launched.url);
  flow.resolve(`${request.searchParams.get("redirect_uri")}?${new URLSearchParams({state: request.searchParams.get("state"), code: "late-code"})}`);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetched, false); assert.equal(auth.signedIn, false);
  assert.equal(chrome.data.unrelatedDashboardPreference, "preserve"); auth.dispose();
});

test("Sign-out aborts a hanging token request and a late response cannot restore authentication", async () => {
  const response = deferred(); let request;
  const {auth, chrome} = setup({fetchImpl: (_, options) => { request = options; return response.promise; }});
  const pending = auth.signIn(), rejected = assert.rejects(pending, {code: "AUTH_CANCELLED"});
  await until(() => request);
  await auth.signOut(); await rejected;
  assert.equal(request.signal.aborted, true);
  response.resolve(tokenResponse()); await new Promise(resolve => setImmediate(resolve));
  assert.equal(auth.signedIn, false);
  assert.ok(!JSON.stringify(chrome.data).includes(jwt())); auth.dispose();
});

test("One window signing out invalidates another window's token and pending sign-in", async () => {
  const chrome = chromeFixture();
  const first = setup({chrome}).auth, second = setup({chrome}).auth;
  await first.signIn(); await second.initialize();
  assert.equal(second.signedIn, true);
  await first.signOut();
  assert.equal(second.signedIn, false); await assert.rejects(second.getAccessToken(), {code: "AUTH_REQUIRED"});
  const flow = deferred(); let launched = false;
  chrome.identity.launchWebAuthFlow = () => { launched = true; return flow.promise; };
  const pending = second.signIn(), rejected = assert.rejects(pending, {code: "AUTH_CANCELLED"});
  await until(() => launched);
  await first.signOut(); await rejected;
  flow.reject(new Error(PRIVATE));
  assert.equal(second.signedIn, false); assert.equal(chrome.data.unrelatedDashboardPreference, "preserve");
  first.dispose(); second.dispose();
});

test("Late storage completion after sign-out cannot resurrect a usable cross-window session", async () => {
  const chrome = chromeFixture(), save = deferred();
  const normalSet = chrome.storage.session.set; let pendingSave;
  chrome.storage.session.set = async values => {
    if (Object.values(values).some(value => value && typeof value === "object" && value.accessToken)) {
      pendingSave = values; await save.promise;
    }
    return normalSet(values);
  };
  const first = setup({chrome}).auth, second = setup({chrome}).auth;
  await second.initialize();
  const signing = first.signIn(), rejected = assert.rejects(signing, {code: "AUTH_CANCELLED"});
  await until(() => pendingSave);
  await second.signOut(); save.resolve(); await rejected;
  assert.equal(first.signedIn, false); assert.equal(second.signedIn, false);
  await assert.rejects(second.getAccessToken(), {code: "AUTH_REQUIRED"});
  first.dispose(); second.dispose();
});

test("Token shape, type, scopes, expiry and grant failures are sanitized", async () => {
  const cases = [() => tokenResponse({token_type: "Basic"}), () => tokenResponse({expires_in: 0}),
    () => tokenResponse({expires_in: "3600"}), () => tokenResponse({expires_in: 86401}),
    () => tokenResponse({scope: "openid analysis/read"}), () => tokenResponse({access_token: PRIVATE}),
    () => tokenResponse({access_token: jwt({token_use: "id"})}), () => tokenResponse({access_token: jwt({client_id: "wrong"})}),
    () => tokenResponse({access_token: jwt({scope: "analysis/read"})}), () => tokenResponse({access_token: jwt({exp: NOW / 1000 + 20})}),
    () => tokenResponse({access_token: "a".repeat(16385)}), () => tokenResponse({error: PRIVATE}, {status: 400}),
    () => { throw new Error(PRIVATE); }];
  for (const produce of cases) {
    const {auth, chrome} = setup({fetchImpl: async () => produce()});
    await assert.rejects(auth.signIn(), error => error.code === "AUTH_FAILED" && !error.message.includes(PRIVATE));
    assert.equal(auth.signedIn, false);
    assert.deepEqual(chrome.data, {unrelatedDashboardPreference: "preserve"}); auth.dispose();
  }
});

test("Non-JSON, redirects and oversized token bodies cannot enter storage", async () => {
  for (const response of [new Response(PRIVATE, {headers: {"Content-Type": "text/html"}}),
    new Response("not-json", {headers: {"Content-Type": "application/json"}}),
    new Response(" ".repeat(65537), {headers: {"Content-Type": "application/json"}}),
    new Response("{}", {headers: {"Content-Type": "application/json", "Content-Length": "65537"}}),
    new Response("", {status: 302, headers: {Location: `https://evil.example/${PRIVATE}`}})]) {
    const {auth, chrome} = setup({fetchImpl: async () => response});
    await assert.rejects(auth.signIn(), error => !error.message.includes(PRIVATE));
    assert.equal(auth.signedIn, false); assert.equal(Object.keys(chrome.data).length, 1); auth.dispose();
  }
});

test("Token exchange timeout aborts hanging response reads and cancels the stream", async () => {
  const nativeSet = globalThis.setTimeout, nativeClear = globalThis.clearTimeout;
  let timeout, reading = false, cancelled = false;
  const fakeTimer = {};
  globalThis.setTimeout = (fn, milliseconds, ...args) => milliseconds === 10_000 ? (timeout = fn, fakeTimer) : nativeSet(fn, milliseconds, ...args);
  globalThis.clearTimeout = timer => { if (timer !== fakeTimer) nativeClear(timer); };
  const stream = new ReadableStream({pull() { reading = true; return new Promise(() => {}); }, cancel() { cancelled = true; }});
  const {auth, chrome} = setup({fetchImpl: async () => new Response(stream, {headers: {"Content-Type": "application/json"}})});
  try {
    const signIn = auth.signIn(), rejected = assert.rejects(signIn, {code: "AUTH_CANCELLED"});
    await until(() => timeout && reading); timeout(); await rejected;
    assert.equal(cancelled, true); assert.equal(auth.signedIn, false);
    assert.equal(Object.keys(chrome.data).length, 1);
  } finally { globalThis.setTimeout = nativeSet; globalThis.clearTimeout = nativeClear; auth.dispose(); }
});

test("Disposal cancels pending work and removes storage listeners without deleting other entries", async () => {
  const {auth, chrome} = setup();
  await auth.initialize(); assert.equal(chrome.listeners.size, 1);
  auth.dispose(); assert.equal(chrome.listeners.size, 0);
  assert.equal(auth.signedIn, false);
  await assert.rejects(auth.initialize(), {code: "AUTH_UNAVAILABLE"});
  assert.deepEqual(chrome.data, {unrelatedDashboardPreference: "preserve"});
});
