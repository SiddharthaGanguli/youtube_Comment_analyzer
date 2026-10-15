// Validate downloaded data before rendering it. No rejected input is echoed.
const STATES = ["QUEUED", "FETCHING", "INFERENCING", "SAVING", "SUCCEEDED", "PARTIAL", "FAILED"];
const LABELS = ["Negative", "Neutral", "Positive"];
const SKIPS = ["empty_text", "no_known_features", "text_too_long"];
const MODEL = ["release_id", "sha256", "preprocessing_sha256", "review_confidence_threshold", "result_schema_version"];
const SAMPLING = ["scope", "order", "include_replies", "requested_max_comments", "has_more", "sample_start", "sample_end"];
const CONFIDENCE = "Highest class score, not calibrated correctness probability";
const fail = () => { throw new Error("The analysis response is invalid"); };
const check = condition => { if (!condition) fail(); };
const text = (value, maximum = 10000) => typeof value === "string" && [...value].length <= maximum;
const nonempty = (value, maximum = 10000) => text(value, maximum) && value.length > 0;
const integer = (value, maximum = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(value) && value >= 0 && value <= maximum;
const probability = value => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
const close = (left, right, tolerance = 1e-7) => Math.abs(left - right) <= tolerance;
const uuid = value => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const videoId = value => typeof value === "string" && /^[A-Za-z0-9_-]{11}$/.test(value);

function shape(value, keys) {
  check(value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key)));
}

function stamp(value) {
  check(typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value));
  const parsed = Date.parse(value);
  check(Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 19) === value.slice(0, 19));
  return parsed;
}

function model(value) {
  shape(value, MODEL);
  check(typeof value.release_id === "string" && /^sr_[a-f0-9]{64}$/.test(value.release_id)
    && typeof value.sha256 === "string" && /^[a-f0-9]{64}$/.test(value.sha256)
    && typeof value.preprocessing_sha256 === "string" && /^[a-f0-9]{64}$/.test(value.preprocessing_sha256)
    && probability(value.review_confidence_threshold) && value.result_schema_version === 1);
}

function sample(value) {
  shape(value, SAMPLING);
  check(value.scope === "public_top_level_comments" && value.order === "time" && value.include_replies === false
    && integer(value.requested_max_comments, 1000) && value.requested_max_comments > 0
    && (value.has_more === null || typeof value.has_more === "boolean"));
  check((value.sample_start === null) === (value.sample_end === null));
  if (value.sample_start !== null) check(stamp(value.sample_start) <= stamp(value.sample_end));
}

export function validateAnalysis(raw) {
  shape(raw, ["analysis_id", "video_id", "state", "created_at", "updated_at", "deadline_at", "expires_at", "cache_hit",
    "model", "sampling", "progress", "result_available", "partial_reason", "failure"]);
  check(uuid(raw.analysis_id) && videoId(raw.video_id) && STATES.includes(raw.state) && typeof raw.cache_hit === "boolean");
  model(raw.model); sample(raw.sampling);
  const created = stamp(raw.created_at), updated = stamp(raw.updated_at), deadline = stamp(raw.deadline_at), expires = stamp(raw.expires_at);
  const available = ["SUCCEEDED", "PARTIAL"].includes(raw.state);
  check(created <= updated && updated < expires && deadline < expires && (STATES.slice(0, 4).includes(raw.state) ? deadline > created : true));
  check(raw.result_available === available && (!raw.cache_hit || available));
  check((raw.state === "PARTIAL") === (raw.partial_reason !== null));
  if (raw.partial_reason !== null) check(nonempty(raw.partial_reason, 256));
  check((raw.state === "FAILED") === (raw.failure !== null));
  if (raw.failure !== null) {
    shape(raw.failure, ["code", "message", "retryable"]);
    check(nonempty(raw.failure.code, 128) && nonempty(raw.failure.message, 2000) && typeof raw.failure.retryable === "boolean");
  }
  shape(raw.progress, ["fetched", "duplicates_removed", "analyzed", "skipped"]);
  const progress = raw.progress;
  check(Object.values(progress).every(value => integer(value, 1000)) && progress.duplicates_removed <= progress.fetched);
  const unique = progress.fetched - progress.duplicates_removed;
  check(unique <= raw.sampling.requested_max_comments && progress.analyzed + progress.skipped <= unique
    && (!available || progress.analyzed + progress.skipped === unique));
  return raw;
}

function comment(row, index, threshold) {
  shape(row, ["index", "id", "author", "published_at", "likes", "text", "normalized_text", "status", "sentiment",
    "sentiment_label", "confidence", "class_probabilities", "needs_review", "skipped_reason"]);
  check(row.index === index && nonempty(row.id, 256) && (row.author === null || text(row.author, 1000))
    && integer(row.likes) && (row.text === null || text(row.text, 5000))
    && (row.normalized_text === null || text(row.normalized_text, 10485760)) && typeof row.needs_review === "boolean");
  stamp(row.published_at);
  if (row.status === "skipped") {
    check(SKIPS.includes(row.skipped_reason) && row.sentiment === null && row.sentiment_label === null
      && row.confidence === null && row.class_probabilities === null && row.needs_review === false);
    if (row.skipped_reason === "text_too_long") check(row.text === null && row.normalized_text === null);
    if (row.skipped_reason === "empty_text") check(row.normalized_text === null || row.normalized_text === "");
    if (row.skipped_reason === "no_known_features") check(nonempty(row.normalized_text, 10485760));
  } else {
    check(row.status === "analyzed" && row.skipped_reason === null && nonempty(row.text, 5000)
      && nonempty(row.normalized_text, 10485760) && integer(row.sentiment_label, 2)
      && row.sentiment === LABELS[row.sentiment_label] && probability(row.confidence));
    shape(row.class_probabilities, LABELS);
    const scores = Object.values(row.class_probabilities);
    check(scores.every(probability) && close(scores.reduce((a, b) => a + b, 0), 1)
      && close(row.confidence, Math.max(...scores)) && close(row.confidence, row.class_probabilities[row.sentiment])
      && row.needs_review === (row.confidence < threshold));
  }
}

export function validateResult(raw, expectedAnalysis) {
  shape(raw, ["schema_version", "state", "fetched_at", "expires_at", "video", "sampling", "duplicates_removed", "model",
    "confidence_definition", "summary", "partial_reason", "comments"]);
  check(raw.schema_version === 1 && ["SUCCEEDED", "PARTIAL"].includes(raw.state) && stamp(raw.fetched_at) < stamp(raw.expires_at)
    && raw.confidence_definition === CONFIDENCE && integer(raw.duplicates_removed, 1000));
  check((raw.state === "PARTIAL") === (raw.partial_reason !== null));
  if (raw.partial_reason !== null) check(nonempty(raw.partial_reason, 256));
  model(raw.model); sample(raw.sampling);
  check(typeof raw.sampling.has_more === "boolean");
  shape(raw.video, ["id", "url", "title", "channel_title", "published_at", "total_comment_count"]);
  check(videoId(raw.video.id) && raw.video.url === `https://www.youtube.com/watch?v=${raw.video.id}`
    && text(raw.video.title) && text(raw.video.channel_title)
    && (raw.video.total_comment_count === null || integer(raw.video.total_comment_count)));
  stamp(raw.video.published_at);
  check(Array.isArray(raw.comments) && raw.comments.length <= raw.sampling.requested_max_comments);
  raw.comments.forEach((row, index) => comment(row, index, raw.model.review_confidence_threshold));
  check(new Set(raw.comments.map(row => row.id)).size === raw.comments.length);
  shape(raw.summary, ["received", "analyzed", "skipped", "coverage", "sentiment_counts", "skipped_by_reason", "needs_review"]);
  const summary = raw.summary, analyzed = raw.comments.filter(row => row.status === "analyzed");
  check([summary.received, summary.analyzed, summary.skipped, summary.needs_review].every(value => integer(value, 1000))
    && summary.received === raw.comments.length && summary.analyzed === analyzed.length
    && summary.received === summary.analyzed + summary.skipped && probability(summary.coverage)
    && close(summary.coverage, summary.received ? summary.analyzed / summary.received : 0, 1e-9)
    && summary.needs_review === analyzed.filter(row => row.needs_review).length);
  shape(summary.sentiment_counts, LABELS); shape(summary.skipped_by_reason, SKIPS);
  for (const label of LABELS) check(integer(summary.sentiment_counts[label], 1000)
    && summary.sentiment_counts[label] === analyzed.filter(row => row.sentiment === label).length);
  for (const reason of SKIPS) check(integer(summary.skipped_by_reason[reason], 1000)
    && summary.skipped_by_reason[reason] === raw.comments.filter(row => row.skipped_reason === reason).length);
  const dates = raw.comments.map(row => stamp(row.published_at));
  check(dates.length ? raw.sampling.sample_start !== null && stamp(raw.sampling.sample_start) === Math.min(...dates)
    && stamp(raw.sampling.sample_end) === Math.max(...dates) : raw.sampling.sample_start === null && raw.sampling.sample_end === null);
  if (expectedAnalysis !== undefined) {
    const expected = validateAnalysis(expectedAnalysis);
    check(expected.result_available && expected.state === raw.state && expected.video_id === raw.video.id
      && stamp(expected.expires_at) <= stamp(raw.expires_at)
      && MODEL.every(key => expected.model[key] === raw.model[key])
      && SAMPLING.every(key => expected.sampling[key] === raw.sampling[key])
      && expected.partial_reason === raw.partial_reason
      && expected.progress.fetched === summary.received + raw.duplicates_removed
      && expected.progress.duplicates_removed === raw.duplicates_removed
      && expected.progress.analyzed === summary.analyzed && expected.progress.skipped === summary.skipped);
  }
  return raw;
}

export function resultToView(raw, expectedAnalysis) {
  validateResult(raw, expectedAnalysis);
  return {video: {id: raw.video.id, url: raw.video.url, title: raw.video.title, channel: raw.video.channel_title,
    fetched: raw.summary.received, skipped: raw.summary.skipped, duplicatesRemoved: raw.duplicates_removed,
    sampleStart: raw.sampling.sample_start, sampleEnd: raw.sampling.sample_end, totalCommentCount: raw.video.total_comment_count},
  comments: raw.comments.map(row => ({id: row.id, author: row.author ?? "Unknown author", text: row.text ?? "",
    sentiment: row.sentiment, confidence: row.confidence, likes: row.likes, publishedAt: row.published_at,
    status: row.status, skippedReason: row.skipped_reason, needsReview: row.needs_review})),
  model: {releaseId: raw.model.release_id, reviewThreshold: raw.model.review_confidence_threshold},
  expiresAt: expectedAnalysis?.expires_at ?? raw.expires_at, state: raw.state, partialReason: raw.partial_reason,
  summary: raw.summary, sampling: raw.sampling};
}

export function validateModelDetails(raw) {
  shape(raw, ["model", "type", "labels", "confidence_definition", "evaluation"]);
  model(raw.model); shape(raw.labels, ["0", "1", "2"]);
  check(raw.type === "TF-IDF + LogisticRegression" && raw.confidence_definition === CONFIDENCE
    && LABELS.every((label, index) => raw.labels[String(index)] === label));
  shape(raw.evaluation, ["test_accuracy", "test_macro_f1", "test_rows", "labels_human_confirmed", "context"]);
  check(probability(raw.evaluation.test_accuracy) && probability(raw.evaluation.test_macro_f1)
    && integer(raw.evaluation.test_rows) && raw.evaluation.test_rows > 0
    && typeof raw.evaluation.labels_human_confirmed === "boolean" && nonempty(raw.evaluation.context, 10000));
  return raw;
}
