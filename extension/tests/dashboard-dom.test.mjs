import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {JSDOM} from "jsdom";
import {JobController} from "../job-controller.mjs";
import {AnalysisClient} from "../analysis-client.mjs";
import {resultToView} from "../result-model.mjs";
import {makeFixtureResult, makeFixtureAnalysis, makeFixtureModelDetails, FIXTURE_VIDEO_URL,
  FIXTURE_VIDEO_ID, FIXTURE_VIDEO_INPUT_URL} from "./fixture-data.mjs";

const markup = await readFile(new URL("../dashboard.html", import.meta.url), "utf8");
let instance = 0;
async function until(check) {
  for (let attempt = 0; attempt < 60; attempt++) {if (check()) return; await new Promise(resolve => setImmediate(resolve));}
  assert.fail("The fixture interface did not reach the expected state");
}
async function setup(t, {scenario = "success", error = null, useTransport = false, synthetic = false} = {}) {
  const dom = new JSDOM(markup, {url: "https://fixture.example/dashboard.html", pretendToBeVisual: true});
  const {window} = dom;
  const originals = Object.fromEntries(["document", "window", "location", "history"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const key of Object.keys(originals)) Object.defineProperty(globalThis, key, {value: window[key], configurable: true});
  window.HTMLDialogElement.prototype.showModal = function() {this.open = true;};
  window.HTMLDialogElement.prototype.close = function() {this.open = false;};
  window.HTMLElement.prototype.scrollIntoView = function() {};
  const app = await import(`../dashboard-app.mjs?dom-test=${instance++}`);
  let now = Date.now();
  const result = makeFixtureResult({scenario, now});
  const analysis = makeFixtureAnalysis({state: result.state, result, now});
  const calls = {submit: 0, status: 0, download: 0, remove: 0};
  const auth = {signedIn: false, async initialize() {}, async signIn() {this.signedIn = true;},
    async getAccessToken() {if (!this.signedIn) throw {code: "AUTH_REQUIRED"}; return "fixture-token";},
    async signOut() {this.signedIn = false;}};
  let api = {async submit() {calls.submit++; if (error) throw error; return analysis;},
    async status() {calls.status++; return analysis;}, async download() {calls.download++; return result;},
    async delete() {calls.remove++;}, async currentModel() {return makeFixtureModelDetails(result);}};
  const requests = [];
  if (useTransport) {
    const config = {enabled: true, apiBaseUrl: "https://fixture.execute-api.us-east-1.amazonaws.com",
      cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com", clientId: "fixtureclient1",
      extensionId: "a".repeat(32), resultBaseUrl: "https://comment-lens-fixture.s3.us-east-1.amazonaws.com/serving/development"};
    const bytes = new TextEncoder().encode(JSON.stringify(result));
    const hash = Buffer.from(await crypto.subtle.digest("SHA-256", bytes)).toString("hex");
    const url = `${config.resultBaseUrl}/work/${analysis.analysis_id}/attempt/1/results/${hash}.json?X-Amz-Signature=synthetic`;
    const response = body => new Response(JSON.stringify(body), {headers: {"Content-Type": "application/json"}});
    api = new AnalysisClient({config, auth, now: () => now, fetchImpl: async (destination, options) => {
      requests.push({destination, options});
      if (destination === url) {
        calls.download++; assert.equal(options.headers?.Authorization, undefined);
        return new Response(bytes, {headers: {"Content-Type": "application/json"}});
      }
      assert.equal(new URL(destination).origin, config.apiBaseUrl);
      assert.equal(options.headers.Authorization, "Bearer fixture-token");
      const path = new URL(destination).pathname;
      if (path === "/v1/analyses") {calls.submit++; return response(analysis);}
      if (path.endsWith("/result")) return response({url, expires_at: new Date(now + 60_000).toISOString(), sha256: hash,
        bytes: bytes.byteLength, content_type: "application/json", schema_version: 1});
      if (path === "/v1/models/current") return response(makeFixtureModelDetails(result));
      assert.fail("Unexpected fixture request");
    }});
  }
  const controller = new JobController({api, auth, now: () => now, isHidden: () => false});
  app.configureService(controller, resultToView, {synthetic});
  await controller.initialize();
  const $ = selector => window.document.querySelector(selector);
  async function signIn() {
    $("#use-service").click();
    $("#accept-policy").checked = true;
    $("#accept-policy").dispatchEvent(new window.Event("change"));
    $("#sign-in").click();
    await until(() => controller.state.signedIn && controller.state.phase === "ready");
  }
  async function submit(videoUrl = FIXTURE_VIDEO_URL) {
    $("#video-url").value = videoUrl;
    $("#video-form").dispatchEvent(new window.Event("submit", {bubbles: true, cancelable: true}));
    await until(() => !controller.state.busy && calls.submit > 0);
  }
  t.after(() => {
    controller.dispose(); dom.window.close();
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  });
  return {$, window, controller, calls, requests, result, signIn, submit, setNow: value => {now = value;}};
}

test("Demo mode keeps its totals and filters, while consent gates service actions", async t => {
  const f = await setup(t);
  assert.equal(f.$("#mode-badge").textContent, "Demo data");
  assert.equal(f.$("#total-stat").textContent, "24");
  f.$('[data-sentiment="Negative"]').click();
  assert.equal(f.$("#comment-rows").children.length, 4);
  f.$("#use-service").click();
  assert.equal(f.$("#analysis-content").hidden, true);
  assert.equal(f.$("#sign-in").disabled, true);
  assert.equal(f.$("#submit-video").disabled, true);
  f.$("#video-url").value = FIXTURE_VIDEO_URL;
  f.$("#video-form").dispatchEvent(new f.window.Event("submit", {cancelable: true}));
  assert.equal(f.calls.submit, 0);
});

test("Checking a demo link explicitly explains that its comments cannot change", async t => {
  const f = await setup(t);
  const rows = f.$("#comment-rows").textContent;
  assert.equal(f.$("#submit-video").textContent, "Check video link");
  assert.ok(f.$("#video-mode-note").textContent.includes("Live comments are not connected"));
  for (const video of [FIXTURE_VIDEO_INPUT_URL, "https://www.youtube.com/watch?v=BnMm_WGV9Rw"]) {
    f.$("#video-url").value = video;
    f.$("#video-form").dispatchEvent(new f.window.Event("submit", {cancelable: true}));
    assert.ok(f.$("#video-feedback").textContent.includes("same handwritten sample comments"));
    assert.equal(f.$("#comment-rows").textContent, rows);
  }
  assert.equal(f.calls.submit, 0);
});

test("Fixture runs keep synthetic labels on results, counts, account controls and comment details", async t => {
  const f = await setup(t, {synthetic: true});
  await f.signIn();
  assert.equal(f.$("#submit-video").textContent, "Simulate test run");
  assert.equal(f.$("#connection-title").textContent, "Fixture account active");
  assert.ok(f.$("#video-mode-note").textContent.includes("always uses fixed synthetic comments"));
  assert.equal(f.$("#demo-banner").hidden, false);
  await f.submit();
  await until(() => Boolean(f.controller.state.modelDetails));
  assert.equal(f.$("#mode-badge").textContent, "Synthetic test data");
  assert.equal(f.$("#sidebar-status").textContent, "Synthetic fixture");
  assert.equal(f.$("#video-eyebrow").textContent, "SYNTHETIC FIXTURE RESULTS");
  assert.equal(f.$("#video-comment-total").textContent, "Synthetic value: 125");
  assert.equal(f.$("#analyzed-stat-label").textContent, "Test comments analyzed");
  assert.ok(f.$("#coverage-note").textContent.includes("not the linked video"));
  assert.ok(f.$("#job-progress").textContent.startsWith("Simulated counts:"));
  assert.equal(f.$("#job-message").textContent, "Synthetic test: The fixed test result is ready.");
  assert.ok(f.$("#model-quality").textContent.includes("synthetic"));
  assert.equal(f.$("#demo-banner").hidden, false);
  f.$("#comment-rows button").click();
  assert.ok(f.$("#dialog-note").textContent.includes("unrelated to the linked video"));
  f.$("#show-demo").click();
  assert.equal(f.$("#submit-video").textContent, "Check video link");
  assert.ok(f.$("#demo-banner-copy").textContent.includes("handwritten sample data"));
});

test("Connected results render one verified sample; filters and details make no extra requests", async t => {
  const f = await setup(t);
  await f.signIn(); await f.submit();
  assert.equal(f.controller.state.phase, "complete");
  assert.equal(f.$("#analysis-content").hidden, false);
  assert.equal(f.$("#total-stat").textContent, String(f.result.summary.analyzed));
  assert.equal(f.$("#skipped-count").textContent, String(f.result.summary.skipped));
  assert.ok(f.$("#comment-rows").textContent.includes("Skipped"));
  assert.ok(f.$("#comment-rows").textContent.includes("Unknown author"));
  assert.equal(f.$("#video-thumbnail").hidden, true);
  const before = {...f.calls};
  f.$("#review-only").checked = true; f.$("#review-only").dispatchEvent(new f.window.Event("change"));
  assert.equal(f.$("#comment-rows").children.length, f.result.summary.needs_review);
  f.$("#reset-filters").click();
  f.$("#comment-rows button").click();
  assert.equal(f.$("#comment-dialog").open, true);
  assert.deepEqual(f.calls, before);
  f.$("#show-demo").click();
  assert.equal(f.$("#comment-dialog").open, false);
  assert.equal(f.$("#dialog-text").textContent, "");
  assert.equal(f.$("#mode-badge").textContent, "Demo data");
});

test("The supplied playlist/radio link reaches the API as one video and renders its verified fixture result", async t => {
  const f = await setup(t, {useTransport: true}); await f.signIn(); await f.submit(FIXTURE_VIDEO_INPUT_URL);
  await until(() => Boolean(f.controller.state.modelDetails));
  assert.equal(f.controller.state.phase, "complete");
  assert.equal(f.calls.submit, 1); assert.equal(f.calls.download, 1);
  assert.equal(f.requests.length, 4);
  assert.equal(f.controller.state.analysis.video_id, FIXTURE_VIDEO_ID);
  assert.equal(f.controller.state.result.video.id, FIXTURE_VIDEO_ID);
  assert.equal(f.$("#video-url").value, FIXTURE_VIDEO_URL);
  const submission = f.requests.find(request => request.options.method === "POST");
  assert.deepEqual(JSON.parse(submission.options.body), {video_url: FIXTURE_VIDEO_URL, max_comments: 1000});
  const blobCreate = URL.createObjectURL, blobRevoke = URL.revokeObjectURL;
  const downloads = [];
  URL.createObjectURL = blob => {downloads.push(blob); return "blob:fixture-result";};
  URL.revokeObjectURL = () => {};
  f.window.HTMLAnchorElement.prototype.click = function() {};
  try {
    f.$("#search").value = "Alex"; f.$("#search").dispatchEvent(new f.window.Event("input"));
    f.$("#export").click();
    assert.equal(downloads.length, 1);
    const csv = await downloads[0].text();
    assert.ok(csv.includes("Alex (fixture)")); assert.ok(!csv.includes("Maya (fixture)"));
    assert.equal(f.requests.length, 4);
  } finally {URL.createObjectURL = blobCreate; URL.revokeObjectURL = blobRevoke;}
});

test("Signing out clears result rows, comment dialogs and export access", async t => {
  const f = await setup(t); await f.signIn(); await f.submit();
  f.$("#comment-rows button").click();
  f.$("#sign-out").click();
  await until(() => f.controller.state.phase === "signed-out");
  assert.equal(f.$("#analysis-content").hidden, true);
  assert.equal(f.$("#comment-rows").children.length, 0);
  assert.equal(f.$("#dialog-text").textContent, "");
  assert.equal(f.$("#dialog-meta").textContent, "");
  assert.equal(f.$("#comment-dialog").open, false);
  assert.equal(f.$("#export").disabled, true);
});

test("Expiry clears owned results even when the shared result has a later expiry", async t => {
  const f = await setup(t); await f.signIn(); await f.submit();
  f.setNow(Date.parse(f.controller.state.analysis.expires_at)); f.controller.expired();
  assert.equal(f.$("#analysis-content").hidden, true);
  assert.equal(f.$("#comment-rows").children.length, 0);
  assert.ok(f.$("#job-message").textContent.includes("expired"));
});

test("Owned deletion uses confirmation and removes the visible reference", async t => {
  const f = await setup(t); await f.signIn(); await f.submit();
  f.$("#remove-analysis").click();
  assert.equal(f.$("#remove-dialog").open, true); assert.equal(f.calls.remove, 0);
  f.$("#cancel-remove").click(); assert.equal(f.calls.remove, 0);
  f.$("#remove-analysis").click(); f.$("#confirm-remove").click();
  await until(() => f.controller.state.phase === "deleted");
  assert.equal(f.calls.remove, 1); assert.equal(f.$("#comment-rows").children.length, 0);
});

for (const [scenario, phase] of [["partial", "partial"], ["empty", "empty"], ["all-skipped", "all-skipped"]]) {
  test(`The ${scenario} result has an explicit usable state without NaN percentages`, async t => {
    const f = await setup(t, {scenario}); await f.signIn(); await f.submit();
    assert.equal(f.controller.state.phase, phase);
    assert.equal(f.$("#analysis-content").hidden, false);
    assert.ok(!f.window.document.body.textContent.includes("NaN"));
    assert.ok(f.$("#job-message").textContent.length > 0);
    if (phase === "all-skipped") assert.ok(f.$("#comment-rows").textContent.includes("Skipped"));
  });
}

test("Quota failures show reset time and never expose provider messages or demo results as live", async t => {
  const resetAt = new Date(Date.now() + 60_000).toISOString();
  const f = await setup(t, {error: {code: "YOUTUBE_QUOTA_EXHAUSTED", message: "provider-private-details", retryable: true, resetAt}});
  await f.signIn(); await f.submit();
  assert.equal(f.$("#analysis-content").hidden, true);
  assert.ok(f.$("#job-message").textContent.includes("allowance"));
  assert.ok(f.$("#job-message").textContent.includes("Available after"));
  assert.ok(!f.window.document.body.textContent.includes("provider-private-details"));
  assert.equal(f.calls.download, 0);
});
