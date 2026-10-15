// Synthetic test data for the developer preview, never packaged for users.
// All factories return fresh JSON-compatible objects. `now` is epoch milliseconds.
export const FIXTURE_VIDEO_ID = "BnMm_WGV9Rw";
export const FIXTURE_VIDEO_URL = `https://www.youtube.com/watch?v=${FIXTURE_VIDEO_ID}`;
// The user's watch-page link includes playlist/radio context. Only the video is submitted.
export const FIXTURE_VIDEO_INPUT_URL = `${FIXTURE_VIDEO_URL}&list=RD${FIXTURE_VIDEO_ID}&start_radio=1`;
export const FIXTURE_SCENARIOS = Object.freeze([
  "success", "partial", "empty", "all-skipped", "quota", "busy", "failed", "expired",
]);
const ANALYSIS_ID = "2a571d3b-40c4-45c2-96bc-6a89e33c9255";
const DAY = 86_400_000;
const iso = value => new Date(value).toISOString();
const model = () => ({release_id: "sr_" + "1".repeat(64), sha256: "2".repeat(64),
  preprocessing_sha256: "3".repeat(64), review_confidence_threshold: .7, result_schema_version: 1});

function comment(index, now, changes = {}) {
  return {index, id: `fixture-comment-${index + 1}`, author: "Fixture viewer",
    published_at: iso(now - DAY - index * 3_600_000), likes: 0,
    text: "A useful explanation.", normalized_text: "a useful explanation", status: "analyzed",
    sentiment: "Positive", sentiment_label: 2, confidence: .9,
    class_probabilities: {Negative: .04, Neutral: .06, Positive: .9}, needs_review: false,
    skipped_reason: null, ...changes};
}

function skipped(index, now, reason, changes = {}) {
  return comment(index, now, {status: "skipped", sentiment: null, sentiment_label: null, confidence: null,
    class_probabilities: null, needs_review: false, skipped_reason: reason, ...changes});
}

export function makeFixtureResult({scenario = "success", now = Date.now(), videoId = FIXTURE_VIDEO_ID} = {}) {
  if (!FIXTURE_SCENARIOS.includes(scenario) || !Number.isSafeInteger(now) || now < 31 * DAY
      || !/^[A-Za-z0-9_-]{11}$/.test(videoId)) throw new Error("Invalid fixture options");
  let comments = [
    comment(0, now, {author: "Maya (fixture)", text: "The examples made this topic much easier to understand. Thank you!",
      normalized_text: "the examples made this topic much easier to understand thank you", likes: 12}),
    comment(1, now, {author: "Alex (fixture)", text: "I lost the explanation when the audio dropped halfway through.",
      normalized_text: "i lost the explanation when the audio dropped halfway through", likes: 4,
      sentiment: "Negative", sentiment_label: 0, confidence: .58,
      class_probabilities: {Negative: .58, Neutral: .3, Positive: .12}, needs_review: true}),
    skipped(2, now, "no_known_features", {author: null, text: "zxqv plm", normalized_text: "zxqv plm", likes: 1}),
  ];
  if (scenario === "empty") comments = [];
  if (scenario === "all-skipped") comments = [
    skipped(0, now, "empty_text", {author: null, text: "!!!", normalized_text: ""}),
    skipped(1, now, "no_known_features", {text: "zxqv plm", normalized_text: "zxqv plm"}),
    skipped(2, now, "text_too_long", {author: null, text: null, normalized_text: null}),
  ];
  const analyzed = comments.filter(row => row.status === "analyzed");
  const dates = comments.map(row => Date.parse(row.published_at));
  const partial = scenario === "partial";
  return {schema_version: 1, state: partial ? "PARTIAL" : "SUCCEEDED", fetched_at: iso(now),
    expires_at: iso(now + (scenario === "expired" ? 15_000 : 3_600_000)),
    video: {id: videoId, url: `https://www.youtube.com/watch?v=${videoId}`,
      title: "Fixture video: a practical project walkthrough", channel_title: "Synthetic preview channel",
      published_at: iso(now - 30 * DAY), total_comment_count: 125},
    sampling: {scope: "public_top_level_comments", order: "time", include_replies: false,
      requested_max_comments: 1000, has_more: partial,
      sample_start: dates.length ? iso(Math.min(...dates)) : null,
      sample_end: dates.length ? iso(Math.max(...dates)) : null},
    duplicates_removed: comments.length ? 1 : 0, model: model(),
    confidence_definition: "Highest class score, not calibrated correctness probability",
    summary: {received: comments.length, analyzed: analyzed.length, skipped: comments.length - analyzed.length,
      coverage: comments.length ? analyzed.length / comments.length : 0,
      sentiment_counts: Object.fromEntries(["Negative", "Neutral", "Positive"].map(label =>
        [label, analyzed.filter(row => row.sentiment === label).length])),
      skipped_by_reason: Object.fromEntries(["empty_text", "no_known_features", "text_too_long"].map(reason =>
        [reason, comments.filter(row => row.skipped_reason === reason).length])),
      needs_review: analyzed.filter(row => row.needs_review).length},
    partial_reason: partial ? "FETCH_RETRIES_EXHAUSTED" : null, comments};
}

export function makeFixtureAnalysis({state, result = makeFixtureResult({scenario: state === "PARTIAL" ? "partial" : "success"}), analysisId = ANALYSIS_ID,
  now = Date.parse(result.fetched_at)} = {}) {
  state ??= result.state;
  const available = ["SUCCEEDED", "PARTIAL"].includes(state);
  const created = Date.parse(result.fetched_at) - 1000;
  const sampling = structuredClone(result.sampling);
  if (!available) {sampling.has_more = null; sampling.sample_start = sampling.sample_end = null;}
  return {analysis_id: analysisId, video_id: result.video.id, state,
    created_at: iso(created), updated_at: iso(Math.max(created, now)),
    deadline_at: iso(Math.min(created + 30 * 60_000, Date.parse(result.expires_at) - 1000)),
    expires_at: result.expires_at, cache_hit: false, model: structuredClone(result.model), sampling,
    progress: available ? {fetched: result.summary.received + result.duplicates_removed,
      duplicates_removed: result.duplicates_removed, analyzed: result.summary.analyzed, skipped: result.summary.skipped}
      : {fetched: state === "FETCHING" ? Math.min(2, result.comments.length) : 0,
        duplicates_removed: 0, analyzed: 0, skipped: 0},
    result_available: available, partial_reason: state === "PARTIAL" ? result.partial_reason : null,
    failure: state === "FAILED" ? {code: "COMMENTS_DISABLED", message: "Comments are disabled for this fixture video.",
      retryable: false} : null};
}

export function makeFixtureModelDetails(result = makeFixtureResult()) {
  return {model: structuredClone(result.model), type: "TF-IDF + LogisticRegression",
    labels: {0: "Negative", 1: "Neutral", 2: "Positive"},
    confidence_definition: result.confidence_definition,
    evaluation: {test_accuracy: .72, test_macro_f1: .7, test_rows: 1200, labels_human_confirmed: false,
      context: "Illustrative fixture values, not a measured model evaluation."}};
}
