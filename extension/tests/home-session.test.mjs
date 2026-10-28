import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {JSDOM} from "jsdom";
import {configureHomeSession} from "../home.mjs";
import {validateWebClientConfig} from "../web-client-config.mjs";
import {readWebSession, writeWebSession, clearWebSession, WEB_SESSION_SKEW_MS} from "../web-session-store.mjs";

const markup = await readFile(new URL("../index.html", import.meta.url), "utf8");
const HOME = "https://siddharthaganguli.github.io/youtube_Comment_analyzer/index.html";
const NOW = 1_800_000_000_000;
const CONFIG = {enabled: true, apiBaseUrl: "https://fixture.execute-api.us-east-1.amazonaws.com",
  cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com", clientId: "fixtureclient1",
  redirectUri: HOME.replace("index.html", "dashboard.html"),
  resultBaseUrl: "https://fixture-bucket.s3.us-east-1.amazonaws.com/serving/development"};
const dependencies = {validateWebClientConfig, readWebSession, clearWebSession, WEB_SESSION_SKEW_MS};
function jwt() {
  const encode = value => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${encode({alg: "RS256"})}.${encode({token_use: "access", client_id: CONFIG.clientId,
    exp: NOW / 1000 + 3600, scope: "openid analysis/read analysis/write"})}.fixture-signature`;
}
function setup(t, href = HOME) {
  const dom = new JSDOM(markup, {url: href});
  let controller;
  t.after(() => {controller?.dispose(); dom.window.close();});
  dom.window.fetch = () => assert.fail("The homepage must not contact the API or provider");
  return {window: dom.window, $: selector => dom.window.document.querySelector(selector),
    async start(options = {}) {
      controller = await configureHomeSession(CONFIG, {runtime: dom.window, now: () => NOW, dependencies, ...options});
      return controller;
    }};
}
function save(window, expiresAtMs = NOW + 120_000) {
  return writeWebSession(CONFIG, {accessToken: jwt(), expiresAtMs}, {storage: window.sessionStorage, now: () => NOW});
}

test("The homepage reflects an existing tab session without exposing its token or extending it", async t => {
  const f = setup(t); save(f.window);
  const before = [...Object.entries(f.window.sessionStorage)];
  await f.start();
  assert.equal(f.$("#home-account-status").hidden, false);
  assert.equal(f.$("#home-sign-out").hidden, false);
  assert.equal(f.$(".home-login").hidden, true);
  assert.equal(f.$(".home-signup").hidden, true);
  assert.ok(!f.window.document.body.textContent.includes(jwt()));
  assert.deepEqual([...Object.entries(f.window.sessionStorage)], before);
});

test("A guest still has working Login and Sign up links, including the directory homepage", async t => {
  const f = setup(t, HOME.replace("index.html", ""));
  await f.start();
  assert.equal(f.$("#home-account-status").hidden, true);
  assert.equal(f.$("#home-sign-out").hidden, true);
  assert.equal(f.$(".home-login").hidden, false);
  assert.equal(f.$(".home-signup").hidden, false);
  assert.equal(f.$(".home-login").getAttribute("href"), "dashboard.html?auth=login#account");
});

test("Homepage Sign out clears its own session and pending login but preserves unrelated storage", async t => {
  const f = setup(t); save(f.window);
  const pending = `commentLens.webPkce.${CONFIG.clientId}.${CONFIG.redirectUri}`;
  f.window.sessionStorage.setItem(pending, "in-flight test login");
  f.window.sessionStorage.setItem("dashboard.preference", "keep");
  await f.start(); f.$("#home-sign-out").click();
  assert.equal(f.$("#home-account-status").hidden, true);
  assert.equal(f.$(".home-login").hidden, false);
  assert.equal(readWebSession(CONFIG, {storage: f.window.sessionStorage, now: () => NOW}), null);
  assert.equal(f.window.sessionStorage.getItem(pending), null);
  assert.equal(f.window.sessionStorage.getItem("dashboard.preference"), "keep");
});

test("The header expires at the original deadline and clears the expired saved session", async t => {
  const f = setup(t); save(f.window);
  let now = NOW, scheduled;
  f.window.setTimeout = (fn, delay) => {scheduled = {fn, delay}; return 1;};
  f.window.clearTimeout = () => {};
  await f.start({now: () => now});
  assert.equal(scheduled.delay, 120_000 - WEB_SESSION_SKEW_MS + 1);
  now += scheduled.delay; scheduled.fn();
  assert.equal(f.$("#home-account-status").hidden, true);
  assert.equal(f.$(".home-login").hidden, false);
  assert.equal(f.window.sessionStorage.length, 0);
});

test("Back/forward restoration checks the current session instead of keeping an old signed-in header", async t => {
  const f = setup(t); save(f.window); await f.start();
  clearWebSession(CONFIG, {storage: f.window.sessionStorage});
  f.window.dispatchEvent(new f.window.PageTransitionEvent("pageshow", {persisted: true}));
  assert.equal(f.$("#home-account-status").hidden, true);
  assert.equal(f.$(".home-login").hidden, false);
});

test("Storage failures do not hide guest account links or falsely confirm a successful sign-out", async t => {
  const f = setup(t); save(f.window);
  await f.start({dependencies: {...dependencies, clearWebSession() {throw new Error("blocked");}}});
  f.$("#home-sign-out").click();
  assert.equal(f.$("#home-account-status").hidden, false);
  assert.equal(f.$("#home-session-message").hidden, false);
  assert.ok(f.$("#home-session-message").textContent.includes("could not be completed"));
  assert.ok(!f.$("#home-session-message").textContent.includes("blocked"));
});

test("A disabled build or different origin cannot read a saved website session", async t => {
  const f = setup(t, "https://other.example/index.html");
  let reads = 0;
  const guarded = {...dependencies, readWebSession() {reads++; assert.fail("Unexpected session read");}};
  assert.equal(await f.start({dependencies: guarded}), null);
  assert.equal(reads, 0);
  assert.equal(await configureHomeSession({enabled: false}, {runtime: f.window, dependencies: guarded}), null);
  assert.equal(reads, 0);
  assert.equal(f.$(".home-login").hidden, false);
});
