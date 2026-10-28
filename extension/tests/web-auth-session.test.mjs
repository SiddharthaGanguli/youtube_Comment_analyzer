import assert from "node:assert/strict";
import test from "node:test";
import {validateWebClientConfig} from "../web-client-config.mjs";
import {WebAuthSession} from "../web-auth-session.mjs";
import {pkceChallenge} from "../auth-pkce.mjs";

const NOW = 1_792_022_400_000;
const PRIVATE = "private-provider-details";
const config = {enabled: true, apiBaseUrl: "https://fixture.execute-api.us-east-1.amazonaws.com",
  cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com", clientId: "fixtureclient1",
  redirectUri: "https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html",
  resultBaseUrl: "https://yt-comment-analyzer-main.s3.us-east-1.amazonaws.com/serving/development"};

function jwt(update = {}) {
  const encode = value => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${encode({alg: "RS256"})}.${encode({token_use: "access", client_id: config.clientId,
    exp: NOW / 1000 + 3600, scope: "openid analysis/read analysis/write", ...update})}.fixture-signature`;
}

function tokenResponse(update = {}, options = {}) {
  return new Response(JSON.stringify({access_token: jwt(), token_type: "Bearer", expires_in: 3600,
    id_token: "discard-this-id-token", refresh_token: "discard-this-refresh-token", ...update}),
  {status: 200, headers: {"Content-Type": "application/json"}, ...options});
}

function storageFixture() {
  const data = new Map([["dashboard.preference", "preserve"]]);
  return {data, getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value),
    removeItem: key => data.delete(key), clear() { throw new Error("Never clear all storage"); }};
}

function setup({storage = storageFixture(), href = config.redirectUri, fetchImpl = async () => tokenResponse(),
  now = () => NOW, config: settings = config, navigate} = {}) {
  const location = {href}, historyCalls = [], navigations = [];
  const history = {replaceState(state, title, value) { historyCalls.push([state, title, value]); location.href = value; }};
  const auth = new WebAuthSession({config: settings, locationApi: location, historyApi: history, storage,
    fetchImpl, now, navigate: navigate ?? (url => navigations.push(url))});
  return {auth, storage, location, historyCalls, navigations};
}

async function until(predicate) {
  const deadline = performance.now() + 2000;
  while (performance.now() < deadline) {
    if (predicate()) return;
    await new Promise(resolve => globalThis.setTimeout(resolve, 2));
  }
  throw new Error("Fixture operation did not reach expected point");
}

async function requestFixture(options = {}, entry = "signIn") {
  const departing = setup(options);
  const signIn = departing.auth[entry]();
  const rejected = assert.rejects(signIn, {code: "AUTH_CANCELLED"});
  await until(() => departing.navigations.length === 1);
  const request = new URL(departing.navigations[0]);
  const pendingKey = [...departing.storage.data.keys()].find(key => key.startsWith("commentLens.webPkce."));
  const pending = JSON.parse(departing.storage.getItem(pendingKey));
  departing.auth.dispose(); await rejected;
  const href = `${request.searchParams.get("redirect_uri")}?${new URLSearchParams({state: request.searchParams.get("state"), code: "fixture-code"})}`;
  return {...departing, request, pendingKey, pending, href};
}

test("Website settings preserve common backend limits and keep extension callback rules separate", () => {
  const checked = validateWebClientConfig(config);
  assert.equal(checked.redirectUri, config.redirectUri);
  assert.equal(Object.hasOwn(checked, "extensionId"), false);
  assert.deepEqual(checked.pollScheduleSeconds, [2, 5, 10]);
  assert.equal(checked.maxComments, 1000);
  assert.ok(Object.isFrozen(checked));
  assert.deepEqual(validateWebClientConfig(), {enabled: false});
  for (const redirectUri of ["http://localhost:8766/dashboard.html", "http://127.0.0.1:8766/dashboard.html", "http://[::1]:8766/dashboard.html"])
    assert.equal(validateWebClientConfig({...config, redirectUri}).redirectUri, redirectUri);
  for (const update of [{enabled: false}, {extensionId: "a".repeat(32)}, {apiKey: PRIVATE},
    {redirectUri: "http://public.example/dashboard.html"}, {redirectUri: "http://127.0.0.2:8766/dashboard.html"},
    {redirectUri: "https://example.test:8443/dashboard.html"}, {redirectUri: `${config.redirectUri}?next=private`},
    {redirectUri: `${config.redirectUri}#section`}, {redirectUri: "https://example.test/a/../dashboard.html"},
    {redirectUri: "https://user:password@example.test/dashboard.html"}, {redirectUri: "https://example.test/%64ashboard.html"},
    {cognitoDomain: "https://provider.example"}, {resultBaseUrl: "https://files.example/serving/development"}, {maxComments: 1001}])
    assert.throws(() => validateWebClientConfig({...config, ...update}));
});

test("A normal website load does not redirect, fetch, or restore a token from storage", async () => {
  let fetched = false;
  const {auth, navigations, storage} = setup({href: `${config.redirectUri}?video=MSNA5bZYvFQ#comments`,
    fetchImpl: async () => { fetched = true; return tokenResponse(); }});
  assert.equal(await auth.initialize(), false);
  assert.equal(auth.signedIn, false); assert.equal(fetched, false); assert.deepEqual(navigations, []);
  await assert.rejects(auth.getAccessToken(), {code: "AUTH_REQUIRED"});
  assert.deepEqual([...storage.data], [["dashboard.preference", "preserve"]]);
  auth.dispose();
});

test("A user sign-in navigates once with fresh S256 state and only a bounded PKCE record", async () => {
  const first = setup();
  const signing = first.auth.signIn(), secondCall = first.auth.signIn();
  assert.equal(signing, secondCall);
  const rejected = assert.rejects(signing, {code: "AUTH_CANCELLED"});
  await until(() => first.navigations.length === 1);
  const url = new URL(first.navigations[0]);
  const stored = JSON.parse([...first.storage.data.values()].find(value => value !== "preserve"));
  assert.deepEqual(Object.keys(stored).sort(), ["createdAtMs", "state", "verifier"]);
  assert.match(stored.state, /^[A-Za-z0-9_-]{43}$/); assert.match(stored.verifier, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(stored.createdAtMs, NOW); assert.equal(url.origin, config.cognitoDomain);
  assert.equal(url.searchParams.get("response_type"), "code");
  assert.equal(url.searchParams.get("scope"), "openid analysis/read analysis/write");
  assert.equal(url.searchParams.get("redirect_uri"), config.redirectUri);
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  assert.equal(url.searchParams.get("code_challenge"), await pkceChallenge(stored.verifier));
  assert.ok(!url.href.includes(stored.verifier)); assert.equal(first.auth.signedIn, false);
  first.auth.dispose(); await rejected;
  // pagehide disposal must preserve the verifier for the returning document.
  assert.equal(first.storage.data.size, 2);
  const fresh = await requestFixture();
  assert.notEqual(fresh.pending.state, stored.state); assert.notEqual(fresh.pending.verifier, stored.verifier);
});

test("Explicit website signup uses the real signup page with fresh PKCE and the same verified callback", async () => {
  const initial = setup({href: `${config.redirectUri}?auth=signup#account`});
  assert.equal(await initial.auth.initialize(), false);
  assert.deepEqual(initial.navigations, []);
  initial.auth.dispose();
  const request = await requestFixture({}, "signUp");
  assert.equal(request.request.origin, config.cognitoDomain);
  assert.equal(request.request.pathname, "/signup");
  assert.equal(request.request.searchParams.get("response_type"), "code");
  assert.equal(request.request.searchParams.get("redirect_uri"), config.redirectUri);
  assert.equal(request.request.searchParams.get("client_id"), config.clientId);
  assert.equal(request.request.searchParams.get("scope"), "openid analysis/read analysis/write");
  assert.equal(request.request.searchParams.get("code_challenge_method"), "S256");
  assert.equal(request.request.searchParams.get("code_challenge"), await pkceChallenge(request.pending.verifier));
  assert.ok(!request.request.href.includes(request.pending.verifier));
  const login = await requestFixture();
  assert.notEqual(request.pending.state, login.pending.state);
  assert.notEqual(request.pending.verifier, login.pending.verifier);
  const callback = setup({storage: request.storage, href: request.href});
  assert.equal(await callback.auth.initialize(), true);
  assert.equal(await callback.auth.getAccessToken(), jwt());
  assert.equal(callback.storage.getItem(request.pendingKey), null);
  callback.auth.dispose();
});

test("Signup shares one user flow and keeps the existing five-minute callback expiry", async () => {
  const first = setup();
  const signup = first.auth.signUp();
  assert.equal(first.auth.signUp(), signup);
  assert.equal(first.auth.signIn(), signup);
  const rejected = assert.rejects(signup, {code: "AUTH_CANCELLED"});
  await until(() => first.navigations.length === 1);
  assert.equal(new URL(first.navigations[0]).pathname, "/signup");
  first.auth.dispose(); await rejected;
  const request = await requestFixture({}, "signUp");
  let exchanges = 0;
  const callback = setup({storage: request.storage, href: request.href, now: () => NOW + 300_001,
    fetchImpl: async () => {exchanges++; return tokenResponse();}});
  await assert.rejects(callback.auth.initialize(), {code: "AUTH_FAILED"});
  assert.equal(exchanges, 0);
  assert.equal(callback.storage.getItem(request.pendingKey), null);
  callback.auth.dispose();
});

test("Returning callback removes private URL parameters before a one-use token exchange", async () => {
  const request = await requestFixture(), exchanges = [];
  const callback = setup({storage: request.storage, href: request.href, fetchImpl: async (url, options) => {
    assert.equal(callback.location.href, config.redirectUri);
    assert.equal(callback.storage.getItem(request.pendingKey), null);
    exchanges.push([url, options]); return tokenResponse();
  }});
  assert.equal(await callback.auth.initialize(), true);
  assert.equal(await callback.auth.getAccessToken(), jwt()); assert.equal(callback.auth.signedIn, true);
  assert.equal(exchanges.length, 1);
  const [url, options] = exchanges[0], body = new URLSearchParams(options.body);
  assert.equal(url, `${config.cognitoDomain}/oauth2/token`); assert.equal(options.method, "POST");
  assert.equal(options.credentials, "omit"); assert.equal(options.redirect, "error"); assert.equal(options.cache, "no-store");
  assert.equal(body.get("code_verifier"), request.pending.verifier); assert.equal(body.get("redirect_uri"), config.redirectUri);
  assert.equal(body.get("client_id"), config.clientId); assert.equal(body.get("grant_type"), "authorization_code");
  assert.ok(options.signal instanceof AbortSignal);
  assert.deepEqual([...callback.storage.data], [["dashboard.preference", "preserve"]]);
  assert.ok(!JSON.stringify([...callback.storage.data]).includes(jwt()));
  callback.auth.dispose();
  const reload = setup({storage: callback.storage});
  assert.equal(await reload.auth.initialize(), false); reload.auth.dispose();
  const replay = setup({storage: callback.storage, href: request.href});
  await assert.rejects(replay.auth.initialize(), {code: "AUTH_FAILED"}); replay.auth.dispose();
});

test("Website sign-in requires an exact configured page origin and path", () => {
  for (const href of ["https://evil.example/dashboard.html", config.redirectUri.replace("/dashboard.html", "/index.html"),
    config.redirectUri.replace("https://", "http://"), "https://user:password@siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html"])
    assert.throws(() => setup({href}), {code: "AUTH_UNAVAILABLE"});
  assert.throws(() => setup({config: {enabled: false}}), {code: "AUTH_UNAVAILABLE"});
});

test("Mismatched, duplicate, malformed and implicit callbacks consume state and never fetch", async () => {
  const mutations = [url => url.searchParams.set("state", "wrong"), url => url.searchParams.append("state", "second"),
    url => url.searchParams.append("code", "second"), url => url.searchParams.set("code", " "),
    url => url.searchParams.set("auth", "signup"),
    url => url.searchParams.set("access_token", PRIVATE), url => { url.hash = `access_token=${PRIVATE}`; },
    url => url.searchParams.set("error_description", PRIVATE), url => {
      url.searchParams.delete("code"); url.searchParams.set("error", "access_denied"); url.searchParams.set("error_description", PRIVATE);
    }];
  for (const mutate of mutations) {
    const request = await requestFixture();
    const url = new URL(request.href); mutate(url); let fetched = false;
    const callback = setup({storage: request.storage, href: url.href, fetchImpl: async () => { fetched = true; return tokenResponse(); }});
    await assert.rejects(callback.auth.initialize(), error => error.code === "AUTH_FAILED" && !error.message.includes(PRIVATE));
    assert.equal(fetched, false); assert.equal(callback.storage.getItem(request.pendingKey), null);
    for (const name of ["code", "state", "error", "error_description", "access_token"]) assert.equal(new URL(callback.location.href).searchParams.has(name), false);
    assert.ok(!callback.location.href.includes("#access_token"));
    callback.auth.dispose();
  }
});

test("Callback cleanup preserves unrelated page parameters and fragments on rejection", async () => {
  const request = await requestFixture();
  const callback = setup({storage: request.storage, href: `${request.href}&video=MSNA5bZYvFQ#comments`});
  await assert.rejects(callback.auth.initialize(), {code: "AUTH_FAILED"});
  assert.equal(callback.location.href, `${config.redirectUri}?video=MSNA5bZYvFQ#comments`);
  callback.auth.dispose();
});

test("An expired, future or tampered pending record cannot exchange a callback", async () => {
  for (const update of [{createdAtMs: NOW - 300_001}, {createdAtMs: NOW + 1}, {verifier: "short"},
    {state: "short"}, {tokenEndpoint: "https://evil.example"}]) {
    const request = await requestFixture(); let fetched = false;
    request.storage.setItem(request.pendingKey, JSON.stringify({...request.pending, ...update}));
    const callback = setup({storage: request.storage, href: request.href, fetchImpl: async () => { fetched = true; return tokenResponse(); }});
    await assert.rejects(callback.auth.initialize(), {code: "AUTH_FAILED"});
    assert.equal(fetched, false); assert.equal(callback.storage.getItem(request.pendingKey), null); callback.auth.dispose();
  }
});

test("Website token checks reject wrong clients, scopes, types and invalid lifetimes without persistence", async () => {
  const cases = [{token_type: "Basic"}, {expires_in: 0}, {expires_in: "3600"}, {expires_in: 86401},
    {scope: "openid analysis/read"}, {access_token: PRIVATE}, {access_token: jwt({token_use: "id"})},
    {access_token: jwt({client_id: "wrong"})}, {access_token: jwt({scope: "analysis/read"})},
    {access_token: jwt({exp: NOW / 1000 + 20})}, {access_token: "a".repeat(16385)}];
  for (const update of cases) {
    const request = await requestFixture();
    const callback = setup({storage: request.storage, href: request.href, fetchImpl: async () => tokenResponse(update)});
    await assert.rejects(callback.auth.initialize(), error => error.code === "AUTH_FAILED" && !error.message.includes(PRIVATE));
    assert.equal(callback.auth.signedIn, false); assert.deepEqual([...callback.storage.data], [["dashboard.preference", "preserve"]]);
    callback.auth.dispose();
  }
});

test("Website token exchange rejects redirects, non-JSON and oversized responses", async () => {
  for (const response of [new Response(PRIVATE, {headers: {"Content-Type": "text/html"}}),
    new Response("not-json", {headers: {"Content-Type": "application/json"}}),
    new Response(" ".repeat(65537), {headers: {"Content-Type": "application/json"}}),
    new Response("{}", {headers: {"Content-Type": "application/json", "Content-Length": "65537"}}),
    new Response("", {status: 302, headers: {Location: `https://evil.example/${PRIVATE}`}})]) {
    const request = await requestFixture();
    const callback = setup({storage: request.storage, href: request.href, fetchImpl: async () => response});
    await assert.rejects(callback.auth.initialize(), error => error.code === "AUTH_FAILED" && !error.message.includes(PRIVATE));
    assert.equal(callback.auth.signedIn, false); callback.auth.dispose();
  }
});

test("A valid memory session expires with skew and sign-out never clears unrelated storage", async () => {
  const request = await requestFixture(); let now = NOW;
  const callback = setup({storage: request.storage, href: request.href, now: () => now});
  await callback.auth.initialize(); now += 3_570_000;
  assert.equal(callback.auth.signedIn, false); await assert.rejects(callback.auth.getAccessToken(), {code: "AUTH_REQUIRED"});
  await callback.auth.signOut();
  assert.deepEqual([...callback.storage.data], [["dashboard.preference", "preserve"]]); callback.auth.dispose();
});

test("Sign-out cancels a pending navigation and removes only its own PKCE attempt", async () => {
  const page = setup();
  const pending = page.auth.signIn(), rejected = assert.rejects(pending, {code: "AUTH_CANCELLED"});
  await until(() => page.navigations.length === 1);
  await page.auth.signOut(); await rejected;
  assert.deepEqual([...page.storage.data], [["dashboard.preference", "preserve"]]);
  assert.equal(page.auth.signedIn, false); page.auth.dispose();
});

test("Sign-out cancels a hanging exchange and a late response cannot restore authentication", async () => {
  const request = await requestFixture(); let resolve, options;
  const delayed = new Promise(done => { resolve = done; });
  const callback = setup({storage: request.storage, href: request.href, fetchImpl: (_url, requestOptions) => { options = requestOptions; return delayed; }});
  const pending = callback.auth.initialize(), rejected = assert.rejects(pending, {code: "AUTH_CANCELLED"});
  await until(() => options);
  await callback.auth.signOut(); await rejected;
  assert.equal(options.signal.aborted, true);
  resolve(tokenResponse()); await new Promise(done => setImmediate(done));
  assert.equal(callback.auth.signedIn, false); callback.auth.dispose();
});

test("The global fetch and timers keep their browser receiver", async t => {
  const request = await requestFixture(); let calls = 0;
  t.mock.method(globalThis, "fetch", async function (_url, options) {
    assert.equal(this, globalThis); assert.equal(options.method, "POST"); calls++; return tokenResponse();
  });
  const location = {href: request.href};
  const auth = new WebAuthSession({config, locationApi: location, historyApi: {replaceState(_state, _title, url) { location.href = url; }},
    storage: request.storage, now: () => NOW});
  assert.equal(await auth.initialize(), true); assert.equal(calls, 1); auth.dispose();
});

test("A token response timeout aborts pending reads and closes the stream", async t => {
  const request = await requestFixture(); let timeout, reading = false, cancelled = false;
  const nativeSet = globalThis.setTimeout, nativeClear = globalThis.clearTimeout, marker = {};
  t.mock.method(globalThis, "setTimeout", function (fn, delay, ...args) {
    assert.equal(this, globalThis);
    return delay === 10_000 ? (timeout = fn, marker) : nativeSet(fn, delay, ...args);
  });
  t.mock.method(globalThis, "clearTimeout", function (id) {
    assert.equal(this, globalThis); if (id !== marker) nativeClear(id);
  });
  const stream = new ReadableStream({pull() { reading = true; return new Promise(() => {}); }, cancel() { cancelled = true; }});
  const callback = setup({storage: request.storage, href: request.href,
    fetchImpl: async () => new Response(stream, {headers: {"Content-Type": "application/json"}})});
  const pending = callback.auth.initialize(), rejected = assert.rejects(pending, {code: "AUTH_CANCELLED"});
  await until(() => timeout && reading); timeout(); await rejected;
  assert.equal(cancelled, true); assert.equal(callback.auth.signedIn, false); callback.auth.dispose();
});

test("Blocked storage or navigation failures cannot leave a usable session", async () => {
  const storage = storageFixture();
  storage.setItem = () => { throw new Error(PRIVATE); };
  const page = setup({storage});
  await assert.rejects(page.auth.signIn(), error => error.code === "AUTH_FAILED" && !error.message.includes(PRIVATE));
  assert.deepEqual(page.navigations, []); page.auth.dispose();
  const broken = setup({navigate() { throw new Error(PRIVATE); }});
  await assert.rejects(broken.auth.signIn(), error => error.code === "AUTH_FAILED" && !error.message.includes(PRIVATE));
  assert.deepEqual([...broken.storage.data], [["dashboard.preference", "preserve"]]); broken.auth.dispose();
});
