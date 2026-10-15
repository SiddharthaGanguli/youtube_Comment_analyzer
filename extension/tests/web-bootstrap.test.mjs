import assert from "node:assert/strict";
import test from "node:test";
import {readFile} from "node:fs/promises";
import {JSDOM} from "jsdom";
import {webClientSettings} from "../web-client-settings.mjs";

const markup = await readFile(new URL("../dashboard.html", import.meta.url), "utf8");
const WEB_URL = "https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html";
const CONFIG = {enabled: true, apiBaseUrl: "https://fixture.execute-api.us-east-1.amazonaws.com",
  cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com", clientId: "fixtureclient1",
  redirectUri: WEB_URL, resultBaseUrl: "https://fixture-bucket.s3.us-east-1.amazonaws.com/serving/development"};
let serial = 0;

async function setup(t, url = WEB_URL) {
  const dom = new JSDOM(markup, {url, pretendToBeVisual: true}), {window} = dom;
  const descriptors = Object.fromEntries(["document", "window", "location", "history"].map(key =>
    [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const key of Object.keys(descriptors)) Object.defineProperty(globalThis, key, {value: window[key], configurable: true});
  window.HTMLDialogElement.prototype.showModal = function() {this.open = true;};
  window.HTMLDialogElement.prototype.close = function() {this.open = false;};
  window.HTMLElement.prototype.scrollIntoView = function() {};
  const module = await import(`../dashboard.mjs?web-bootstrap=${serial++}`);
  const received = {auth: [], api: [], controller: [], initialize: 0, dispose: 0, reload: 0};
  const runtime = {location: {href: window.location.href, reload() {received.reload++;}},
    history: window.history, sessionStorage: window.location.protocol === "chrome-extension:" ? undefined : window.sessionStorage,
    addEventListener: window.addEventListener.bind(window)};
  class WebAuthSession {
    constructor(options) {received.auth.push(options);}
  }
  class AnalysisClient {
    constructor(options) {
      assert.equal(options.configValidator(options.config).enabled, true);
      received.api.push(options);
    }
  }
  class JobController {
    constructor(options) {
      received.controller.push(options);
      this.state = {phase: "signed-out", signedIn: false, busy: false, analysis: null, result: null, error: null, modelDetails: null};
    }
    async initialize() {received.initialize++; this.onChange(this.state);}
    async syncSession() {}
    async resume() {}
    pause() {}
    dispose() {received.dispose++;}
  }
  const dependencies = {WebAuthSession, AnalysisClient, JobController, resultToView: value => value};
  t.after(() => {
    window.close();
    for (const [key, descriptor] of Object.entries(descriptors)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  });
  return {window, module, runtime, received, dependencies, $: selector => window.document.querySelector(selector)};
}

test("The public source stays disabled and shows configuration feedback without loading an account", async t => {
  assert.deepEqual(webClientSettings, {enabled: false}); assert.ok(Object.isFrozen(webClientSettings));
  const f = await setup(t);
  assert.equal(await f.module.connectWebService(webClientSettings, {runtime: f.runtime, dependencies: f.dependencies}), null);
  assert.equal(f.received.auth.length, 0); assert.equal(f.received.api.length, 0);
  assert.equal(f.$("#video-feedback").hidden, false);
});

test("Configured website starts the real workspace and wires web auth through the strict shared client", async t => {
  const f = await setup(t);
  const controller = await f.module.connectWebService(CONFIG, {runtime: f.runtime, dependencies: f.dependencies});
  assert.ok(controller); assert.equal(f.received.initialize, 1);
  assert.equal(f.received.auth[0].config.redirectUri, WEB_URL);
  assert.equal(f.received.auth[0].storage, f.window.sessionStorage);
  assert.equal(f.received.api[0].auth instanceof f.dependencies.WebAuthSession, true);
  assert.equal(Object.hasOwn(f.received.api[0].config, "extensionId"), false);
  assert.deepEqual(f.received.controller[0].pollScheduleSeconds, [2, 5, 10]);
  assert.equal(f.$("#mode-badge").textContent, "Analysis workspace");
  assert.equal(f.$("#analysis-content").hidden, true);
  assert.equal(f.$("#policy-consent").hidden, false);
  assert.equal(f.$("#sign-in").disabled, true);
});

test("Cross-origin or different-page callbacks fail before constructing auth or issuing a request", async t => {
  const f = await setup(t);
  for (const redirectUri of ["https://evil.example/dashboard.html", WEB_URL.replace("dashboard.html", "index.html")]) {
    const controller = await f.module.connectWebService({...CONFIG, redirectUri}, {runtime: f.runtime, dependencies: f.dependencies});
    assert.equal(controller, null);
  }
  assert.equal(f.received.auth.length, 0); assert.equal(f.received.api.length, 0);
  assert.equal(f.$("#video-feedback").hidden, false);
});

test("A navigating web session is disposed and BFcache restoration starts a clean page", async t => {
  const f = await setup(t);
  assert.ok(await f.module.connectWebService(CONFIG, {runtime: f.runtime, dependencies: f.dependencies}));
  f.window.dispatchEvent(new f.window.PageTransitionEvent("pagehide", {persisted: true}));
  assert.equal(f.received.dispose, 1);
  f.window.dispatchEvent(new f.window.PageTransitionEvent("pageshow", {persisted: true}));
  assert.equal(f.received.reload, 1);
});

test("The extension's disabled branch stays separate from the website settings", async t => {
  const f = await setup(t, `chrome-extension://${"a".repeat(32)}/dashboard.html`);
  assert.equal(f.$("#video-feedback").hidden, true);
  assert.equal(f.received.auth.length, 0);
});
