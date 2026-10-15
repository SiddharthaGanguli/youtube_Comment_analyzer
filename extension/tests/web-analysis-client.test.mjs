import assert from "node:assert/strict";
import test from "node:test";
import {AnalysisClient} from "../analysis-client.mjs";
import {validateWebClientConfig} from "../web-client-config.mjs";
import {makeFixtureAnalysis, makeFixtureResult, FIXTURE_VIDEO_INPUT_URL, FIXTURE_VIDEO_URL} from "./fixture-data.mjs";

const NOW = Date.parse("2026-10-15T10:00:00Z");
const KEY = "3a571d3b-40c4-45c2-96bc-6a89e33c9255";
const TOKEN = "synthetic-private-access-token";
const config = {enabled: true, apiBaseUrl: "https://fixture.execute-api.us-east-1.amazonaws.com/release",
  cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com", clientId: "fixtureclient1",
  redirectUri: "https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html",
  resultBaseUrl: "https://yt-comment-analyzer-main.s3.us-east-1.amazonaws.com/serving/development"};

test("Explicit website validation permits one authenticated canonical video submission", async () => {
  const calls = [];
  const queued = makeFixtureAnalysis({state: "QUEUED", result: makeFixtureResult({now: NOW}), now: NOW});
  const client = new AnalysisClient({config, configValidator: validateWebClientConfig,
    auth: {getAccessToken: async () => TOKEN}, now: () => NOW,
    fetchImpl: async (url, options) => {
      calls.push({url, options});
      return new Response(JSON.stringify(queued), {status: 202, headers: {"Content-Type": "application/json"}});
    }});
  assert.deepEqual(await client.submit(FIXTURE_VIDEO_INPUT_URL, KEY), queued);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${config.apiBaseUrl}/v1/analyses`);
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), {video_url: FIXTURE_VIDEO_URL, max_comments: 1000});
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(calls[0].options.headers["Idempotency-Key"], KEY);
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.redirect, "error");
  assert.equal(calls[0].options.referrerPolicy, "no-referrer");
  assert.equal(Object.hasOwn(client.config, "extensionId"), false);
});

test("Website validator rejects unsafe destinations before reading a token or issuing requests", () => {
  let tokens = 0, requests = 0;
  const auth = {getAccessToken: async () => { tokens++; return TOKEN; }};
  const fetchImpl = async () => { requests++; throw new Error("Unexpected fixture request"); };
  for (const update of [{apiBaseUrl: "http://public.example"}, {apiBaseUrl: "https://user:private@api.example"},
    {apiBaseUrl: "https://api.example/release?credential=private"}, {apiBaseUrl: "https://api.example/release/../private"},
    {cognitoDomain: "https://provider.example"}, {resultBaseUrl: "https://files.example/serving/development"},
    {resultBaseUrl: "https://yt-comment-analyzer-main.s3.us-east-1.amazonaws.com/private"},
    {redirectUri: "http://public.example/dashboard.html"}, {redirectUri: `${config.redirectUri}#token`},
    {redirectUri: "https://user:private@example.test/dashboard.html"}, {extensionId: "a".repeat(32)}]) {
    assert.throws(() => new AnalysisClient({config: {...config, ...update}, configValidator: validateWebClientConfig, auth, fetchImpl}));
  }
  assert.equal(tokens, 0); assert.equal(requests, 0);
});

test("The default extension validator continues to reject website client settings", () => {
  let requests = 0;
  assert.throws(() => new AnalysisClient({config, auth: {getAccessToken: async () => TOKEN},
    fetchImpl: async () => { requests++; throw new Error("Unexpected fixture request"); }}));
  assert.equal(requests, 0);
});
