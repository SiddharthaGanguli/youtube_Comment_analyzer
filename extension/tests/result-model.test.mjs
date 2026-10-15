import assert from "node:assert/strict";
import test from "node:test";
import {validateAnalysis, validateResult, validateModelDetails, resultToView} from "../result-model.mjs";

const TIME = "2026-10-15T10:00:00Z", EXPIRY = "2026-10-16T10:00:00Z";
const model = () => ({release_id: "sr_" + "1".repeat(64), sha256: "2".repeat(64), preprocessing_sha256: "3".repeat(64),
  review_confidence_threshold: .7, result_schema_version: 1});
const sampling = () => ({scope: "public_top_level_comments", order: "time", include_replies: false,
  requested_max_comments: 1000, has_more: false, sample_start: TIME, sample_end: TIME});
const row = () => ({index: 0, id: "fixture-comment", author: null, published_at: TIME, likes: 2,
  text: "Helpful context", normalized_text: "helpful context", status: "analyzed", sentiment: "Positive", sentiment_label: 2,
  confidence: .65, class_probabilities: {Negative: .15, Neutral: .2, Positive: .65}, needs_review: true, skipped_reason: null});
const result = () => ({schema_version: 1, state: "SUCCEEDED", fetched_at: TIME, expires_at: EXPIRY,
  video: {id: "dQw4w9WgXcQ", url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ", title: "Fixture video",
    channel_title: "Fixture channel", published_at: TIME, total_comment_count: null}, sampling: sampling(), duplicates_removed: 1,
  model: model(), confidence_definition: "Highest class score, not calibrated correctness probability",
  summary: {received: 1, analyzed: 1, skipped: 0, coverage: 1, sentiment_counts: {Negative: 0, Neutral: 0, Positive: 1},
    skipped_by_reason: {empty_text: 0, no_known_features: 0, text_too_long: 0}, needs_review: 1}, partial_reason: null, comments: [row()]});
const status = () => ({analysis_id: "2a571d3b-40c4-45c2-96bc-6a89e33c9255", video_id: "dQw4w9WgXcQ", state: "SUCCEEDED",
  created_at: TIME, updated_at: TIME, deadline_at: "2026-10-15T10:30:00Z", expires_at: EXPIRY, cache_hit: false,
  model: model(), sampling: sampling(), progress: {fetched: 2, duplicates_removed: 1, analyzed: 1, skipped: 0},
  result_available: true, partial_reason: null, failure: null});

test("Result adaptation preserves measured counts, nullable authors and the actual review threshold", () => {
  const data = result(); assert.equal(validateResult(data, status()), data);
  const view = resultToView(data);
  assert.equal(view.video.fetched, 1); assert.equal(view.video.duplicatesRemoved, 1);
  assert.equal(view.video.sampleStart, TIME); assert.equal(view.video.totalCommentCount, null);
  assert.equal(view.comments[0].author, "Unknown author");
  assert.equal(view.comments[0].needsReview, true);
  assert.equal(view.model.reviewThreshold, .7); assert.equal(view.state, "SUCCEEDED");
});

test("Queued, failed, partial, empty and all-skipped analyses have distinct valid shapes", () => {
  const queued = status(); queued.state = "QUEUED"; queued.result_available = false;
  queued.progress = {fetched: 0, duplicates_removed: 0, analyzed: 0, skipped: 0};
  assert.equal(validateAnalysis(queued), queued);
  const failed = {...queued, state: "FAILED", failure: {code: "COMMENTS_DISABLED", message: "Comments are disabled", retryable: false}};
  assert.equal(validateAnalysis(failed), failed);
  const partial = result(); partial.state = "PARTIAL"; partial.partial_reason = "FETCH_RETRIES_EXHAUSTED";
  assert.equal(validateResult(partial), partial);
  const empty = result(); empty.comments = []; empty.duplicates_removed = 0;
  empty.sampling.sample_start = empty.sampling.sample_end = null;
  empty.summary = {received: 0, analyzed: 0, skipped: 0, coverage: 0, sentiment_counts: {Negative: 0, Neutral: 0, Positive: 0},
    skipped_by_reason: {empty_text: 0, no_known_features: 0, text_too_long: 0}, needs_review: 0};
  assert.equal(validateResult(empty), empty);
  for (const reason of ["empty_text", "no_known_features", "text_too_long"]) {
    const skipped = result(); skipped.comments[0] = {...row(), status: "skipped", sentiment: null, sentiment_label: null,
      confidence: null, class_probabilities: null, needs_review: false, skipped_reason: reason,
      text: reason === "text_too_long" ? null : "", normalized_text: reason === "no_known_features" ? "unknown" : null};
    skipped.summary.analyzed = 0; skipped.summary.skipped = 1; skipped.summary.coverage = 0;
    skipped.summary.sentiment_counts.Positive = 0; skipped.summary.skipped_by_reason[reason] = 1; skipped.summary.needs_review = 0;
    assert.equal(validateResult(skipped), skipped);
    assert.equal(resultToView(skipped).comments[0].text, "");
  }
});

test("Contradictory statuses, non-finite counts, invalid times and unknown fields fail safely", () => {
  const changes = [raw => raw.progress.analyzed = 3, raw => raw.progress.fetched = NaN,
    raw => raw.analysis_id = "not-an-id", raw => raw.video_id = "short", raw => raw.state = "UNKNOWN",
    raw => raw.result_available = false, raw => raw.failure = {code: "x", message: "private", retryable: false},
    raw => raw.partial_reason = "unexpected", raw => raw.expires_at = TIME, raw => raw.updated_at = EXPIRY,
    raw => raw.created_at = "2026-02-30T10:00:00Z", raw => raw.created_at = "2026-10-15T10:00:00",
    raw => raw.owner = "private-person", raw => raw.model.sha256 = "bad"];
  for (const change of changes) { const raw = status(); change(raw); assert.throws(() => validateAnalysis(raw),
    {message: "The analysis response is invalid"}); }
});

test("Results reject inconsistent summaries, predictions, duplicate IDs and fabricated sample periods", () => {
  const changes = [raw => raw.summary.received = 2, raw => raw.summary.coverage = .5,
    raw => raw.summary.sentiment_counts.Positive = 0, raw => raw.summary.skipped_by_reason.empty_text = 1,
    raw => raw.summary.needs_review = 0, raw => raw.comments[0].needs_review = false,
    raw => raw.comments[0].confidence = .9, raw => raw.comments[0].sentiment_label = 0,
    raw => raw.comments[0].class_probabilities.Positive = Infinity,
    raw => raw.comments[0].class_probabilities.Negative = .7, raw => raw.comments[0].index = 1,
    raw => raw.comments.push({...raw.comments[0], index: 1}), raw => raw.comments[0].text = "x".repeat(5001),
    raw => raw.sampling.sample_start = "2026-10-14T10:00:00Z", raw => raw.sampling.has_more = null,
    raw => raw.video.url = "https://evil.example/", raw => raw.video.total_comment_count = -1,
    raw => raw.comments[0].status = "skipped", raw => raw.state = "PARTIAL", raw => raw.schema_version = 2];
  for (const change of changes) { const raw = result(); change(raw); assert.throws(() => validateResult(raw),
    {message: "The analysis response is invalid"}); }
});

test("Downloaded data must match the owned status model, video, sampling and progress", () => {
  for (const change of [raw => raw.video_id = "aaaaaaaaaaa", raw => raw.model.release_id = "sr_" + "9".repeat(64),
    raw => raw.progress.duplicates_removed = 0, raw => raw.expires_at = "2026-10-16T11:00:00Z",
    raw => raw.sampling.requested_max_comments = 999, raw => {raw.state = "PARTIAL"; raw.partial_reason = "FETCH_RETRIES_EXHAUSTED";}]) {
    const expected = status(); change(expected); assert.throws(() => validateResult(result(), expected));
  }
});

test("Unicode code points and normalization expansion match the backend's text rules", () => {
  const raw = result();
  raw.comments[0].text = "😀".repeat(4999) + "x";
  raw.comments[0].normalized_text = "x".repeat(10000);
  assert.equal(validateResult(raw), raw);
  raw.comments[0].text += "x";
  assert.throws(() => validateResult(raw));
});

test("An owned reference may expire before a shared result; it cannot outlive that result", () => {
  const expected = status(); expected.expires_at = "2026-10-15T11:00:00Z";
  assert.equal(validateResult(result(), expected).expires_at, EXPIRY);
  const view = resultToView(result(), expected);
  assert.equal(view.expiresAt, expected.expires_at);
  assert.deepEqual(view.sampling, sampling());
  const other = status(); other.video_id = "aaaaaaaaaaa";
  assert.throws(() => resultToView(result(), other));
  expected.expires_at = "2026-10-17T10:00:00Z";
  assert.throws(() => validateResult(result(), expected));
});

test("Model details preserve supplied-label caveats and reject fabricated metrics", () => {
  const details = {model: model(), type: "TF-IDF + LogisticRegression", labels: {0: "Negative", 1: "Neutral", 2: "Positive"},
    confidence_definition: "Highest class score, not calibrated correctness probability",
    evaluation: {test_accuracy: .71, test_macro_f1: .70, test_rows: 100, labels_human_confirmed: false, context: "Supplied labels; review pending"}};
  assert.equal(validateModelDetails(details), details);
  for (const change of [raw => raw.evaluation.test_accuracy = 1.5, raw => raw.labels[0] = "Positive",
    raw => raw.evaluation.test_rows = 0, raw => raw.evaluation.labels_human_confirmed = "true"]) {
    const invalid = structuredClone(details); change(invalid); assert.throws(() => validateModelDetails(invalid));
  }
});
