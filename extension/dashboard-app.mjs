import { demo } from "./demo-data.mjs";
import { LABELS, youtubeVideoUrl, summarize, filterComments, commentsCsv, trendBuckets, isReview } from "./lib.mjs";

const $ = selector => document.querySelector(selector);
let view = demo, isDemo = true, syntheticService = false, controller, resultAdapter, renderedResult;
let summary = summarize(view.comments);
const state = { sentiment: "All", query: "", reviewOnly: false, sort: "likes", page: 1 };
const pageSize = 6;
const percent = value => `${(value * 100).toFixed(1)}%`;
const date = value => new Date(value).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const fullDate = value => new Date(value).toLocaleString("en-US", {timeZone: "UTC", timeZoneName: "short"});
function skipCopy(reason) {return ({empty_text: "Empty after cleaning", no_known_features: "No known model vocabulary", text_too_long: "Text exceeds the size limit"})[reason] || "Not analyzed";}
let toastTimeout;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function initializeSummary() {
  const fixture = !isDemo && syntheticService;
  summary = summarize(view.comments);
  $("#video-title").textContent = fixture ? "Synthetic comment analysis test" : view.video.title;
  $("#video-channel").textContent = fixture ? "No comments were fetched from this video" : view.video.channel;
  $("#video-card").setAttribute("aria-label", isDemo ? "Selected demo video" : fixture ? "Synthetic fixture results" : "Selected video");
  $("#video-eyebrow").textContent = isDemo ? "DEMO VIDEO" : fixture ? "SYNTHETIC FIXTURE RESULTS" : "PUBLIC COMMENT SAMPLE";
  $("#video-thumbnail").hidden = !isDemo;
  $("#analyzed-stat-label").textContent = fixture ? "Test comments analyzed" : "Comments analyzed";
  $("#total-stat").textContent = summary.total;
  $("#coverage-stat").textContent = `of ${view.video.fetched} ${fixture ? "synthetic comments" : "unique received"} · ${percent(view.video.fetched ? summary.total / view.video.fetched : 0)} coverage`;
  $("#nav-count").textContent = view.comments.length;
  $("#chart-count").textContent = `${summary.total} ${fixture ? "test comments " : ""}analyzed`;
  $("#donut-total").textContent = summary.total;
  $("#review-count").textContent = summary.needsReview;
  const threshold = view.model.reviewThreshold ?? .6;
  $("#review-explanation").textContent = `Scores below ${Math.round(threshold * 100)}% may reflect mixed or ambiguous sentiment.`;
  $("#review-filter-label").textContent = `Only comments below ${Math.round(threshold * 100)}% confidence`;
  $("#legend").replaceChildren();
  const share = label => summary.total ? summary.counts[label] / summary.total : 0;
  for (const label of LABELS) {
    const key = label.toLowerCase();
    $(`#${key}-stat`).replaceChildren(document.createTextNode((share(label) * 100).toFixed(1)), element("span", "", "%"));
    $(`#${key}-count`).textContent = `${summary.counts[label]} ${fixture ? "test " : ""}comments`;
    const row = element("div", "legend-row");
    row.append(element("i", `${key}-bg`), element("span", "", label),
               element("strong", "", percent(share(label))),
               element("small", "", summary.counts[label]));
    $("#legend").append(row);
  }
  const positiveEnd = share("Positive") * 100;
  const neutralEnd = positiveEnd + share("Neutral") * 100;
  $("#donut").style.background = summary.total ? `conic-gradient(var(--positive) 0% ${positiveEnd}%, var(--neutral) ${positiveEnd}% ${neutralEnd}%, var(--negative) ${neutralEnd}% 100%)` : "var(--border)";
  $("#donut").setAttribute("aria-label", LABELS.map(label => `${summary.counts[label]} ${label.toLowerCase()}`).join(", ") + " comments");
  $("#fetched-count").textContent = view.video.fetched;
  $("#video-comment-total").textContent = isDemo ? "Sample fixture" : fixture ? `Synthetic value: ${view.video.totalCommentCount ?? "Not available"}` : view.video.totalCommentCount ?? "Not available";
  $("#analyzed-count").textContent = summary.total;
  $("#skipped-count").textContent = view.video.skipped;
  const period = trendBuckets(view.comments);
  const range = period.length ? `${date(period[0].start)} – ${date(period.at(-1).end)} (UTC)` : "No comment dates";
  $("#sample-period").textContent = range;
  $("#video-sample-period").textContent = range;
  $("#coverage-note").textContent = isDemo ? "These totals describe the demo sample." : fixture ? "These counts describe fixed synthetic comments, not the linked video. Changing the URL only tests the request flow." : `Latest top-level public comments; replies are excluded. ${view.video.duplicatesRemoved || 0} duplicates removed. ${view.sampling?.has_more ? "More comments may be available." : "This is a sample, not a measure of the whole audience."}`;
  $("#model-release").textContent = isDemo ? "V1 baseline" : fixture ? "Synthetic model release" : view.model.releaseId;
  $("#model-quality").textContent = isDemo ? `Held-out test accuracy: ${percent(demo.model.testAccuracy)} · Macro F1: ${percent(demo.model.testMacroF1)} · ${demo.model.testRows.toLocaleString()} test comments. These are dataset results, not the accuracy of this video.` : fixture ? "Model scores and evaluation numbers in this test are synthetic." : "Evaluation for this release is not available here. These are independent model estimates, not YouTube metrics.";
  $("#skipped-breakdown").textContent = isDemo ? "4 empty sample comments are excluded from the demo table." : Object.entries(view.summary?.skipped_by_reason || {}).map(([reason, count]) => `${skipCopy(reason)}: ${count}`).join(" · ");
  $("#sample-expiry").textContent = isDemo ? "Sample data stays in your browser." : `${fixture ? "Synthetic test result" : "Access"} expires ${fullDate(view.expiresAt)}. ${view.partialReason ? `Partial sample: ${view.partialReason.replaceAll("_", " ")}.` : ""}`;
  $("#comments-subtitle").textContent = fixture ? "Fixed synthetic comments for testing the interface." : isDemo ? "Explore handwritten sample comments." : "Read what viewers are saying, in their own words.";
}

function renderTrend() {
  const counts = trendBuckets(view.comments);
  const maximum = Math.max(3, Math.ceil(Math.max(0, ...counts.map(day => day.total)) / 3) * 3);
  $("#trend").replaceChildren();
  $("#trend-period").textContent = `${counts.length} UTC periods`;
  const axis = element("div", "trend-axis");
  for (let step = 3; step >= 0; step--) axis.append(element("span", "", maximum / 3 * step));
  $("#trend").append(axis);
  counts.forEach((day, index) => {
    const column = element("div", "trend-day");
    const bar = element("div", "trend-bar");
    const label = day.start === day.end ? date(day.start) : `${date(day.start)}–${date(day.end)}`;
    column.title = `${label}: ${day.counts.Positive} positive, ${day.counts.Neutral} neutral, ${day.counts.Negative} negative`;
    for (const label of [...LABELS].reverse()) {
      const segment = element("span", `${label.toLowerCase()}-bg`);
      segment.style.height = `${day.counts[label] / maximum * 100}%`;
      bar.append(segment);
    }
    column.append(bar, element("span", "trend-day-label", label));
    $("#trend").append(column);
  });
  $("#trend").setAttribute("aria-label", counts.map((day, index) =>
    `${fullDate(day.start)} through ${fullDate(day.end)}: ${day.counts.Positive} positive, ${day.counts.Neutral} neutral, ${day.counts.Negative} negative comments`).join(". ") || "No analyzed comments to chart");
}

function renderFilters() {
  const focusedLabel = document.activeElement?.dataset.sentiment;
  $(".filters").replaceChildren();
  for (const label of ["All", ...LABELS]) {
    const button = element("button", `filter${state.sentiment === label ? " active" : ""}`, label);
    button.type = "button";
    button.dataset.sentiment = label;
    button.append(element("span", "", label === "All" ? view.comments.length : summary.counts[label]));
    button.setAttribute("aria-pressed", String(state.sentiment === label));
    button.addEventListener("click", () => { state.sentiment = label; state.page = 1; renderFilters(); renderComments(); });
    $(".filters").append(button);
    if (focusedLabel === label) button.focus({ preventScroll: true });
  }
}

function openComment(comment) {
  $("#dialog-author").textContent = comment.author || "Unknown author";
  $("#dialog-text").textContent = comment.text || "No retained display text.";
  $("#dialog-meta").replaceChildren(
    element("span", `sentiment-pill ${comment.sentiment?.toLowerCase() || "skipped"}`, comment.sentiment || "Skipped"),
    element("span", "", comment.confidence == null ? skipCopy(comment.skippedReason) : `${Math.round(comment.confidence * 100)}% highest class score`),
    element("span", "", `${comment.likes} likes`),
    element("span", "", date(comment.publishedAt)),
  );
  $("#dialog-note").textContent = isDemo ? "This is a handwritten sample comment." : syntheticService ? "This comment and its sentiment scores are synthetic test data, unrelated to the linked video." : "Sentiment is generated independently by Comment Lens and may be incorrect. Skipped comments have no prediction.";
  $("#comment-dialog").showModal();
}

function renderComments() {
  const filtered = filterComments(view.comments, state);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  state.page = Math.min(state.page, pageCount);
  const start = (state.page - 1) * pageSize;
  $("#comment-rows").replaceChildren();
  for (const comment of filtered.slice(start, start + pageSize)) {
    const row = element("tr");
    const content = element("div", "comment-cell");
    const copy = element("div");
    const author = comment.author || "Unknown author";
    copy.append(element("div", "comment-author", author), element("p", "comment-text", comment.text || (comment.skippedReason === "text_too_long" ? "Text not retained (size limit)." : "No display text.")));
    content.append(element("span", "avatar", author.trim().split(/\s+/).slice(0, 2).map(part => part[0]).join("")), copy);
    const textCell = element("td");
    textCell.append(content);
    const sentimentCell = element("td");
    sentimentCell.append(element("span", `sentiment-pill ${comment.sentiment?.toLowerCase() || "skipped"}`, comment.sentiment || "Skipped"));
    const confidenceCell = element("td");
    const confidence = element("div", `confidence${isReview(comment) ? " low" : ""}`);
    const track = element("span", "confidence-track");
    track.setAttribute("aria-hidden", "true");
    const fill = element("span");
    fill.style.width = `${(comment.confidence ?? 0) * 100}%`;
    track.append(fill);
    confidence.append(element("span", "", comment.confidence == null ? "—" : `${Math.round(comment.confidence * 100)}%`));
    if (comment.confidence != null) confidence.append(track);
    confidenceCell.append(confidence);
    const detailCell = element("td");
    const detail = element("button", "row-detail", "↗");
    detail.type = "button";
    detail.setAttribute("aria-label", `View comment by ${author}`);
    detail.addEventListener("click", () => openComment(comment));
    detailCell.append(detail);
    row.append(textCell, sentimentCell, confidenceCell, element("td", "", comment.likes),
               element("td", "", date(comment.publishedAt)), detailCell);
    $("#comment-rows").append(row);
  }
  $("#empty-state").hidden = filtered.length !== 0;
  $("#result-badge").textContent = `${filtered.length} ${!isDemo && syntheticService ? "synthetic " : ""}${filtered.length === 1 ? "result" : "results"}`;
  $("#page-info").textContent = filtered.length ? `Showing ${start + 1}–${Math.min(start + pageSize, filtered.length)} of ${filtered.length} comments` : "Showing 0 comments";
  $("#page-number").textContent = `${state.page} / ${pageCount}`;
  $("#previous").disabled = state.page === 1;
  $("#next").disabled = state.page === pageCount;
  $("#reset-filters").hidden = state.sentiment === "All" && !state.query && !state.reviewOnly && state.sort === "likes";
}

function clearFilters() {
  Object.assign(state, { sentiment: "All", query: "", reviewOnly: false, sort: "likes", page: 1 });
  $("#search").value = "";
  $("#review-only").checked = false;
  $("#sort").value = "likes";
  renderFilters();
  renderComments();
}

$("#search").addEventListener("input", event => { state.query = event.target.value; state.page = 1; renderComments(); });
$("#sort").addEventListener("change", event => { state.sort = event.target.value; state.page = 1; renderComments(); });
$("#review-only").addEventListener("change", event => { state.reviewOnly = event.target.checked; state.page = 1; renderComments(); });
$("#previous").addEventListener("click", () => { state.page--; renderComments(); });
$("#next").addEventListener("click", () => { state.page++; renderComments(); });
$("#reset-filters").addEventListener("click", clearFilters);
$("#empty-reset").addEventListener("click", clearFilters);
$("#show-review").addEventListener("click", () => {
  clearFilters();
  state.reviewOnly = true;
  $("#review-only").checked = true;
  renderComments();
  $("#comments").scrollIntoView({ behavior: "smooth" });
  $("#review-only").focus({ preventScroll: true });
});
$("#close-dialog").addEventListener("click", () => $("#comment-dialog").close());
$("#export").addEventListener("click", () => {
  if (!isDemo && (!controller?.state.result || controller.expired())) return;
  const comments = filterComments(view.comments, state);
  const blob = new Blob([commentsCsv(comments)], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = element("a");
  link.href = url;
  link.download = isDemo ? "comment-lens-demo-comments.csv" : syntheticService ? "comment-lens-synthetic-comments.csv" : "comment-lens-comments.csv";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  $("#toast").textContent = `Exported ${comments.length} ${isDemo ? "sample " : syntheticService ? "synthetic " : ""}comments with the current filters.`;
  $("#toast").hidden = false;
  window.clearTimeout(toastTimeout);
  toastTimeout = window.setTimeout(() => { $("#toast").hidden = true; }, 3500);
});
$("#video-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = $("#video-url");
  const video = youtubeVideoUrl(input.value.trim());
  const feedback = $("#video-feedback");
  feedback.hidden = false;
  feedback.classList.toggle("error", !video);
  input.setAttribute("aria-invalid", String(!video));
  if (!video) { feedback.textContent = "Paste a valid HTTPS YouTube watch, Shorts, live, or youtu.be link."; return; }
  input.value = video;
  if (!isDemo) {
    if ($("#accept-policy").checked) { feedback.hidden = true; await controller.submit(video); }
    return;
  }
  const url = new URL(location.href);
  url.searchParams.set("video", video);
  history.replaceState(null, "", url);
  feedback.textContent = `Valid video link: ${video}. Live comments are not connected on this page. The dashboard shows the same handwritten sample comments.`;
});

for (const link of document.querySelectorAll(".nav-link")) {
  link.addEventListener("click", () => {
    for (const sibling of document.querySelectorAll(".nav-link")) {
      sibling.classList.remove("active"); sibling.removeAttribute("aria-current");
    }
    link.classList.add("active"); link.setAttribute("aria-current", "location");
  });
}

const selected = youtubeVideoUrl(new URL(location.href).searchParams.get("video"));
if (selected) $("#video-url").value = selected;
initializeSummary();
renderTrend();
renderFilters();
renderComments();

const ERROR_COPY = {
  AUTH_CANCELLED: "Sign-in was cancelled. You can try again.", AUTH_FAILED: "Sign-in could not finish. Please try again.",
  AUTH_UNAVAILABLE: "Secure sign-in is unavailable in this build. Demo mode remains available.",
  UNAUTHORIZED: "Your session ended. Sign in again to continue.", AUTH_REQUIRED: "Sign in to start an analysis.",
  INVALID_INPUT: "Use a valid HTTPS YouTube video link.", FORBIDDEN: "Your account cannot perform this action.",
  NOT_FOUND: "This analysis is no longer available in your history.",
  RATE_LIMITED: "Please wait before trying again.", ACTIVE_JOB_LIMIT: "Your active analysis limit has been reached.",
  SYSTEM_BUSY: "The service is busy. Please try again shortly.", YOUTUBE_QUOTA_EXHAUSTED: "The daily YouTube allowance has been used.",
  LIVE_ANALYSIS_DISABLED: "Live analysis is not available yet. You can still explore the demo.",
  DEPENDENCY_UNAVAILABLE: "The service is temporarily unavailable.", NETWORK_ERROR: "The connection was interrupted. You can retry safely.",
  REQUEST_TIMEOUT: "The request timed out. You can retry safely.", DOWNLOAD_FAILED: "The result download was interrupted. You can retry it.",
  INVALID_RESPONSE: "The service returned a result that could not be verified.", RESULT_INVALID: "The result could not be verified.",
  RESULT_NOT_READY: "The result is still being prepared.", ANALYSIS_FAILED: "This analysis could not finish.",
  IDEMPOTENCY_CONFLICT: "This submission could not be retried. Start a new analysis.",
};
const PHASE_COPY = {
  initializing: "Checking your sign-in session…",
  "signed-out": "Sign in to analyze a video. Demo mode remains available.", ready: "Paste a public video link to start an analysis.",
  "signing-in": "Complete sign-in in the secure account window.", submitting: "Submitting your analysis…",
  QUEUED: "Your analysis is queued.", FETCHING: "Reading a bounded sample of public comments…",
  INFERENCING: "Estimating sentiment in the sampled comments…", SAVING: "Preparing your result…",
  "loading-result": "Verifying your result…", complete: "Your sample is ready.", partial: "Part of the sample is ready. See the sampling details below.",
  empty: "No comments were returned for this sample.", "all-skipped": "Comments were received, but none could be analyzed.",
  failed: "This analysis could not finish. You may remove it or start another.", expired: "This result has expired. Start a new analysis.",
  deleted: "This analysis was removed from your history.", paused: "Progress checks are paused while you explore the demo.",
};
const FIXTURE_PHASE_COPY = {
  initializing: "Checking the simulated session…",
  "signed-out": "Use the fixture account to simulate a test run.", ready: "Paste a video link to test the request flow with fixed comments.",
  "signing-in": "Activating the simulated account.", submitting: "Starting the simulated request…",
  QUEUED: "The test run is queued.", FETCHING: "Simulating comment fetching; no comments are being downloaded.",
  INFERENCING: "Loading synthetic sentiment scores; the model is not running.", SAVING: "Preparing the synthetic result…",
  "loading-result": "Checking the synthetic result…", complete: "The fixed test result is ready.",
  partial: "The partial-result test scenario is ready.", empty: "The empty-result test scenario is ready.",
  "all-skipped": "The all-skipped test scenario is ready.", failed: "The failure test scenario completed.",
  expired: "The synthetic result expired. Simulate another test run.", deleted: "The synthetic result was removed.",
  paused: "The test run is paused while you explore the demo.",
};

function renderData() {initializeSummary(); renderTrend(); clearFilters();}
function clearDetail() {
  $("#comment-dialog").close();
  $("#remove-dialog").close();
  for (const id of ["#dialog-author", "#dialog-text", "#dialog-meta", "#dialog-note"]) $(id).replaceChildren();
}
function renderConnection(status = controller?.state) {
  const fixture = !isDemo && syntheticService;
  $("#mode-badge").textContent = isDemo ? "Demo data" : fixture ? "Synthetic test data" : "Analysis workspace";
  $("#sidebar-status").textContent = isDemo ? "Demo preview" : fixture ? "Synthetic fixture" : "Connected service";
  $("#demo-banner").hidden = !isDemo && !fixture;
  $("#demo-banner-copy").textContent = fixture ? "Synthetic test mode. The comments, counts, account and model scores are fixed test data. No YouTube or AWS service is called." : "You're viewing a design preview. Charts and comments use handwritten sample data. Live comments are not connected on this page.";
  $("#video-mode-note").textContent = isDemo ? "Live comments are not connected on this page. Changing the link does not change the handwritten sample comments." : fixture ? "Changing the URL only tests the request flow. This preview always uses fixed synthetic comments and cannot load comments from that video." : "Analyze a bounded sample of public comments from the video you submit.";
  $("#connection-panel").hidden = !controller;
  $("#use-service").hidden = !controller || !isDemo;
  $("#use-service").textContent = syntheticService ? "Open fixture workspace" : "Use analysis service";
  $("#show-demo").hidden = isDemo;
  $("#sign-in").hidden = isDemo || Boolean(status?.signedIn);
  $("#sign-out").hidden = isDemo || !status?.signedIn;
  $("#sign-in").textContent = syntheticService ? "Use fixture account" : "Sign in";
  $("#sign-out").textContent = syntheticService ? "End fixture session" : "Sign out";
  $("#policy-consent").hidden = isDemo;
  $("#sign-in").disabled = Boolean(status?.busy) || !$("#accept-policy").checked;
  $("#submit-video").textContent = isDemo ? "Check video link" : fixture ? "Simulate test run" : "Analyze comments";
  $("#submit-video").disabled = !isDemo && (status?.busy || !status?.signedIn || !$("#accept-policy").checked);
  $("#connection-title").textContent = isDemo ? "Demo workspace" : fixture ? status?.signedIn ? "Fixture account active" : "Synthetic test workspace" : status?.signedIn ? "Signed in" : "Your analysis workspace";
  $("#connection-copy").textContent = fixture ? "This account and all results are simulated locally. No real sign-in or comment fetching takes place." : isDemo ? "Explore sample data, or switch to your configured analysis service." : "Sign in with your Comment Lens account. Signing out clears this session; it does not close your account.";
  $("#job-status").hidden = isDemo;
  $("#analysis-content").hidden = !isDemo && !status?.result;
  $("#export").disabled = !isDemo && !status?.result;
  $("#remove-analysis").hidden = isDemo || !status?.analysis;
  $("#remove-analysis").textContent = syntheticService ? "Remove test run" : "Remove from my history";
  $("#remove-analysis").disabled = status?.phase === "submitting" || status?.phase === "loading-result";
  $("#retry-analysis").hidden = isDemo || !status?.error?.retryable || !status?.signedIn || status.analysis?.state === "FAILED";
  if (isDemo || !status) return;
  const error = status.error;
  const wait = error?.resetAt ? ` Available after ${fullDate(error.resetAt)}.` : error?.retryAfterSeconds ? ` Wait at least ${error.retryAfterSeconds} seconds.` : "";
  const message = error ? `${fixture ? "Simulated error: " : ""}${(ERROR_COPY[error.code] || "This action could not complete. Please try again.") + wait}` : (fixture ? FIXTURE_PHASE_COPY : PHASE_COPY)[status.phase] || (fixture ? "Preparing the synthetic test…" : "Preparing your analysis…");
  $("#job-message").textContent = `${fixture ? "Synthetic test: " : ""}${message}`;
  const progress = status.analysis?.progress;
  $("#job-progress").textContent = progress ? `${fixture ? "Simulated counts: " : ""}${progress.fetched} fetched · ${progress.analyzed} analyzed · ${progress.skipped} skipped` : "";
  $("#job-status").classList.toggle("error", Boolean(error));
  $("#job-status").setAttribute("aria-busy", String(status.busy));
  if (status.result && renderedResult !== status.result) {
    renderedResult = status.result; view = resultAdapter(status.result, status.analysis); renderData();
  }
  if (status.modelDetails && status.result) {
    const evaluation = status.modelDetails.evaluation;
    $("#model-quality").textContent = fixture ? "Model scores and evaluation numbers in this test are synthetic." : `This model's held-out accuracy: ${percent(evaluation.test_accuracy)} · Macro F1: ${percent(evaluation.test_macro_f1)} · ${evaluation.test_rows.toLocaleString()} comments. ${evaluation.labels_human_confirmed ? "Labels reviewed." : "Dataset labels are not human-confirmed."} These are dataset measures, not this video's accuracy.`;
  }
  if (!status.result && renderedResult) {
    clearDetail();
    $("#comment-rows").replaceChildren();
    $("#dialog-author").textContent = "";
    $("#dialog-text").textContent = "";
    $("#dialog-meta").replaceChildren();
    $("#dialog-note").textContent = "";
    $("#video-title").textContent = "";
    $("#video-channel").textContent = "";
    $("#model-quality").textContent = "";
    view = demo; renderedResult = null;
  }
}
export function showUnavailable() {
  $("#video-feedback").hidden = false;
  $("#video-feedback").textContent = "Live analysis is not connected on this page yet. You can explore the labeled demo below.";
}
export function configureService(nextController, adapter, {synthetic = false, startInService = false} = {}) {
  syntheticService = synthetic;
  isDemo = !startInService;
  controller = nextController; resultAdapter = adapter; controller.onChange = renderConnection; controller.suspended = !startInService;
  if (syntheticService) {
    $("#remove-title").textContent = "Remove this test run?";
    $("#remove-dialog > p").textContent = "This removes the synthetic result from this preview.";
    $("#confirm-remove").textContent = "Remove test run";
    $("#cancel-remove").textContent = "Keep test run";
    $("#retry-analysis").textContent = "Retry test run";
  }
  $("#accept-policy").addEventListener("change", () => renderConnection());
  $("#sign-in").addEventListener("click", () => {if ($("#accept-policy").checked) controller.signIn();});
  $("#sign-out").addEventListener("click", () => controller.signOut());
  $("#use-service").addEventListener("click", async () => {isDemo = false; renderConnection(); await controller.resume();});
  $("#show-demo").addEventListener("click", () => {controller.pause(); clearDetail(); isDemo = true; view = demo; renderedResult = null; renderData(); renderConnection();});
  $("#retry-analysis").addEventListener("click", () => controller.retry());
  $("#remove-analysis").addEventListener("click", () => $("#remove-dialog").showModal());
  $("#cancel-remove").addEventListener("click", () => $("#remove-dialog").close());
  $("#confirm-remove").addEventListener("click", () => {$("#remove-dialog").close(); controller.remove();});
  document.addEventListener("visibilitychange", () => controller.visibilityChanged());
  window.addEventListener("pageshow", async event => {if (event.persisted) {await controller.syncSession(); if (!isDemo) await controller.resume();}});
  renderConnection();
}
