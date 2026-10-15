import assert from "node:assert/strict";
import test from "node:test";
import {AnalysisClient, ApiError} from "../analysis-client.mjs";

const ID = "2a571d3b-40c4-45c2-96bc-6a89e33c9255", RETRY = "3a571d3b-40c4-45c2-96bc-6a89e33c9255";
const TIME = "2026-10-15T10:00:00Z", EXPIRY = "2026-10-16T10:00:00Z", TOKEN = "synthetic-private-access-token";
const CONFIG = {enabled: true, apiBaseUrl: "https://api.example.com/release",
  cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com", clientId: "fixtureclient1",
  extensionId: "a".repeat(32), resultBaseUrl: "https://fixture-bucket.s3.us-east-1.amazonaws.com/serving/development"};
const model = () => ({release_id: "sr_" + "1".repeat(64), sha256: "2".repeat(64), preprocessing_sha256: "3".repeat(64),
  review_confidence_threshold: .6, result_schema_version: 1});
const sampling = () => ({scope: "public_top_level_comments", order: "time", include_replies: false,
  requested_max_comments: 1000, has_more: false, sample_start: null, sample_end: null});
const status = (state = "SUCCEEDED") => ({analysis_id: ID, video_id: "dQw4w9WgXcQ", state, created_at: TIME, updated_at: TIME,
  deadline_at: "2026-10-15T10:30:00Z", expires_at: EXPIRY, cache_hit: false, model: model(), sampling: sampling(),
  progress: {fetched: 0, duplicates_removed: 0, analyzed: 0, skipped: 0}, result_available: state === "SUCCEEDED",
  partial_reason: null, failure: null});
const result = () => ({schema_version: 1, state: "SUCCEEDED", fetched_at: TIME, expires_at: EXPIRY,
  video: {id: "dQw4w9WgXcQ", url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ", title: "Fixture video",
    channel_title: "Fixture channel", published_at: TIME, total_comment_count: null}, sampling: sampling(), duplicates_removed: 0,
  model: model(), confidence_definition: "Highest class score, not calibrated correctness probability",
  summary: {received: 0, analyzed: 0, skipped: 0, coverage: 0, sentiment_counts: {Negative: 0, Neutral: 0, Positive: 0},
    skipped_by_reason: {empty_text: 0, no_known_features: 0, text_too_long: 0}, needs_review: 0}, partial_reason: null, comments: []});
const reply = (data, status = 200, headers = {}) => new Response(JSON.stringify(data),
  {status, headers: {"Content-Type": "application/json", ...headers}});

function setup(responses, {auth = {getAccessToken: async () => TOKEN}, config = CONFIG, now = () => Date.parse(TIME)} = {}) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({url, options});
    const response = responses.shift();
    if (response instanceof Error) throw response;
    if (typeof response === "function") return response(url, options);
    assert.ok(response, "Unexpected request in fixture transport");
    return response;
  };
  return {client: new AnalysisClient({config, auth, fetchImpl, now}), calls};
}

async function objectFixture(data = result()) {
  const bytes = new TextEncoder().encode(JSON.stringify(data));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha256 = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
  return {bytes, data, descriptor: {url: `${CONFIG.resultBaseUrl}/work/${ID}/attempt/1/results/${sha256}.json?X-Amz-Signature=synthetic`,
    expires_at: "2026-10-15T10:05:00Z", sha256, bytes: bytes.byteLength, content_type: "application/json", schema_version: 1}};
}

test("Submissions canonicalize the URL and send one retry key without automatic POST retries", async () => {
  const {client, calls} = setup([reply(status("QUEUED"), 202)]);
  const accepted = await client.submit("https://youtu.be/dQw4w9WgXcQ?t=3", RETRY);
  assert.equal(accepted.state, "QUEUED"); assert.equal(calls.length, 1);
  assert.equal(calls[0].url, CONFIG.apiBaseUrl + "/v1/analyses");
  assert.deepEqual(JSON.parse(calls[0].options.body), {video_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ", max_comments: 1000});
  assert.equal(calls[0].options.headers["Idempotency-Key"], RETRY);
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(calls[0].options.credentials, "omit"); assert.equal(calls[0].options.redirect, "error");
  const failed = setup([new TypeError("private " + TOKEN)]);
  await assert.rejects(failed.client.submit("https://youtu.be/dQw4w9WgXcQ", RETRY), error =>
    error instanceof ApiError && error.code === "NETWORK_ERROR" && !String(error).includes(TOKEN));
  assert.equal(failed.calls.length, 1);
});

test("Invalid inputs and disabled settings cannot issue an authenticated request", async () => {
  const {client, calls} = setup([]);
  for (const url of ["https://user:private@youtube.com/watch?v=dQw4w9WgXcQ", "http://youtu.be/dQw4w9WgXcQ",
    "https://youtube.com:8443/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ&v=dQw4w9WgXcQ",
    "https://evil.example/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ/another", " ", "x".repeat(1025)]) {
    await assert.rejects(client.submit(url, RETRY), {code: "INVALID_INPUT"});
  }
  await assert.rejects(client.submit("https://youtu.be/dQw4w9WgXcQ", "bad-key"), {code: "INVALID_INPUT"});
  await assert.rejects(client.status("bad-id"), {code: "INVALID_INPUT"});
  await assert.rejects(client.delete("bad-id"), {code: "INVALID_INPUT"});
  assert.equal(calls.length, 0);
  assert.throws(() => setup([], {config: {enabled: false}}), {code: "CONFIGURATION_REQUIRED"});
});

test("Owned status, model details and deletion use only fixed authenticated paths", async () => {
  const details = {model: model(), type: "TF-IDF + LogisticRegression", labels: {0: "Negative", 1: "Neutral", 2: "Positive"},
    confidence_definition: "Highest class score, not calibrated correctness probability",
    evaluation: {test_accuracy: .71, test_macro_f1: .7, test_rows: 100, labels_human_confirmed: false, context: "Supplied labels"}};
  const {client, calls} = setup([reply(status()), reply(details), new Response(null, {status: 204})]);
  assert.deepEqual(await client.status(ID), status()); assert.deepEqual(await client.currentModel(), details);
  assert.equal(await client.delete(ID), undefined);
  assert.deepEqual(calls.map(call => [call.url, call.options.method]), [[CONFIG.apiBaseUrl + `/v1/analyses/${ID}`, "GET"],
    [CONFIG.apiBaseUrl + "/v1/models/current", "GET"], [CONFIG.apiBaseUrl + `/v1/analyses/${ID}`, "DELETE"]]);
});

test("Gateway and application failures use safe messages and bounded retry metadata", async () => {
  const {client} = setup([reply({message: "private " + TOKEN}, 401), reply({message: "private"}, 429, {"Retry-After": "7"}),
    reply({error: {code: "YOUTUBE_QUOTA_EXHAUSTED", message: TOKEN, retryable: true, retry_after_seconds: 90000,
      reset_at: "2026-10-16T07:00:00Z"}}, 503), reply({error: {code: TOKEN, message: TOKEN}}, 503)]);
  await assert.rejects(client.status(ID), error => error.code === "UNAUTHORIZED" && !String(error).includes(TOKEN));
  await assert.rejects(client.status(ID), error => error.code === "RATE_LIMITED" && error.retryAfterSeconds === 7);
  await assert.rejects(client.status(ID), error => error.code === "YOUTUBE_QUOTA_EXHAUSTED" && error.retryable
    && error.retryAfterSeconds === 90000 && error.resetAt === "2026-10-16T07:00:00Z" && !String(error).includes(TOKEN));
  await assert.rejects(client.status(ID), error => error.code === "DEPENDENCY_UNAVAILABLE" && !String(error).includes(TOKEN));
});

test("Auth failures and malformed tokens never reach the configured API", async () => {
  for (const auth of [{getAccessToken: async () => {throw new Error(TOKEN);}}, {getAccessToken: async () => TOKEN + "\r\nInjected: yes"},
    {getAccessToken: async () => null}]) {
    const {client, calls} = setup([], {auth}); await assert.rejects(client.status(ID), error =>
      error.code === "UNAUTHORIZED" && !String(error).includes(TOKEN)); assert.equal(calls.length, 0);
  }
});

test("Malformed JSON, oversized streams, redirected responses and foreign status IDs fail closed", async () => {
  const foreign = status(); foreign.analysis_id = RETRY;
  const redirected = reply(status()); Object.defineProperty(redirected, "redirected", {value: true});
  const foreignUrl = reply(status()); Object.defineProperty(foreignUrl, "url", {value: "https://evil.example/"});
  const responses = [new Response("<html>private</html>"), new Response("x", {headers: {"Content-Length": "65537"}}),
    new Response("x".repeat(65537)), reply(foreign), redirected, foreignUrl];
  for (const response of responses) {
    const {client} = setup([response]); await assert.rejects(client.status(ID), {code: "INVALID_RESPONSE"});
  }
});

test("Successful API data requires JSON media type; native non-JSON gateway failures remain safe", async () => {
  for (const media of [null, "text/html", "text/plain", "application/javascript"] ) {
    const headers = media === null ? {} : {"Content-Type": media};
    const {client} = setup([new Response(new TextEncoder().encode(JSON.stringify(status())), {headers})]);
    await assert.rejects(client.status(ID), {code: "INVALID_RESPONSE"});
  }
  const valid = setup([reply(status(), 200, {"Content-Type": "Application/JSON; charset=utf-8"})]);
  assert.deepEqual(await valid.client.status(ID), status());
  const gateway = setup([new Response("<html>private " + TOKEN + "</html>", {status: 503, headers: {"Content-Type": "text/html"}})]);
  await assert.rejects(gateway.client.status(ID), error => error.code === "DEPENDENCY_UNAVAILABLE" && !String(error).includes(TOKEN));
});

test("Downloads verify the immutable descriptor, SHA256 and result binding without forwarding the bearer", async () => {
  const fixture = await objectFixture();
  const {client, calls} = setup([reply(fixture.descriptor), new Response(fixture.bytes)]);
  assert.deepEqual(await client.download(ID, {analysis: status()}), fixture.data);
  assert.equal(calls.length, 2); assert.equal(calls[0].options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(calls[1].url, fixture.descriptor.url);
  assert.equal(calls[1].options.headers.Authorization, undefined);
  assert.equal(calls[1].options.credentials, "omit"); assert.equal(calls[1].options.redirect, "error");
});

test("Download without supplied status checks ownership and terminal state first", async () => {
  const fixture = await objectFixture();
  const {client, calls} = setup([reply(status()), reply(fixture.descriptor), new Response(fixture.bytes)]);
  assert.deepEqual(await client.download(ID), fixture.data); assert.equal(calls.length, 3);
  assert.ok(calls[0].url.endsWith(`/v1/analyses/${ID}`));
  const waiting = setup([]); await assert.rejects(waiting.client.download(ID, {analysis: status("QUEUED")}), {code: "RESULT_NOT_READY"});
  assert.equal(waiting.calls.length, 0);
});

test("Expired descriptors and S3 authorization expiry renew once without resubmitting analysis", async () => {
  const fixture = await objectFixture();
  for (const reason of ["expired", "forbidden"]) {
    const expired = {...fixture.descriptor, expires_at: TIME};
    const first = reason === "expired" ? [reply(expired)] : [reply(fixture.descriptor), new Response(TOKEN, {status: 403,
      headers: {"Content-Length": "999999999"}})];
    const {client, calls} = setup([...first, reply({...fixture.descriptor, url: fixture.descriptor.url + "-renewed"}), new Response(fixture.bytes)]);
    assert.deepEqual(await client.download(ID, {analysis: status()}), fixture.data);
    assert.equal(calls.filter(call => call.url.includes("/v1/analyses/")).length, 2);
    assert.ok(calls.every(call => call.options.method === "GET"));
  }
  const repeated = setup([reply(fixture.descriptor), new Response(null, {status: 403}), reply(fixture.descriptor), new Response(null, {status: 403})]);
  await assert.rejects(repeated.client.download(ID, {analysis: status()}), {code: "DOWNLOAD_FAILED"});
  assert.equal(repeated.calls.length, 4);
});

test("Malicious or oversized result destinations never receive a token or any request", async () => {
  const fixture = await objectFixture();
  const cases = [{...fixture.descriptor, url: fixture.descriptor.url.replace("fixture-bucket.s3.us-east-1.amazonaws.com", "evil.example")},
    {...fixture.descriptor, url: fixture.descriptor.url.replace("/serving/development/", "/datasets/private/")},
    {...fixture.descriptor, url: fixture.descriptor.url.replace("/results/", "/checkpoints/")},
    {...fixture.descriptor, bytes: 10485761}, {...fixture.descriptor, sha256: "a".repeat(64)},
    {...fixture.descriptor, url: fixture.descriptor.url + "#fragment"}, {...fixture.descriptor, schema_version: 2}];
  for (const descriptor of cases) {
    const {client, calls} = setup([reply(descriptor)]);
    await assert.rejects(client.download(ID, {analysis: status()}), {code: "INVALID_RESPONSE"});
    assert.equal(calls.length, 1);
  }
});

test("Truncation, hash corruption and a validly hashed result for another model are rejected", async () => {
  const fixture = await objectFixture();
  for (const body of [fixture.bytes.slice(0, -1), new Uint8Array(fixture.bytes.length).fill(32),
    new Uint8Array(fixture.bytes.length + 1).fill(32)]) {
    const {client} = setup([reply(fixture.descriptor), new Response(body)]);
    await assert.rejects(client.download(ID, {analysis: status()}), {code: "INVALID_RESPONSE"});
  }
  const foreign = result(); foreign.model.release_id = "sr_" + "9".repeat(64);
  const other = await objectFixture(foreign);
  const {client} = setup([reply(other.descriptor), new Response(other.bytes)]);
  await assert.rejects(client.download(ID, {analysis: status()}), {code: "INVALID_RESPONSE"});
});

test("Cancellation stops requests and a stalled transport respects the client deadline", async t => {
  const cancelled = new AbortController(); cancelled.abort();
  const {client, calls} = setup([]);
  await assert.rejects(client.status(ID, {signal: cancelled.signal}), {code: "REQUEST_ABORTED"}); assert.equal(calls.length, 0);
  t.mock.timers.enable({apis: ["setTimeout"]});
  const stalled = setup([() => new Promise(() => {})]);
  const rejection = assert.rejects(stalled.client.status(ID), {code: "REQUEST_TIMEOUT"});
  await Promise.resolve(); await Promise.resolve();
  t.mock.timers.tick(10001);
  await rejection; assert.equal(stalled.calls.length, 1);
});
