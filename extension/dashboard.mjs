import { demo } from "./demo-data.mjs";
import { LABELS, youtubeVideoUrl, summarize, filterComments, commentsCsv } from "./lib.mjs";

const $ = selector => document.querySelector(selector);
const summary = summarize(demo.comments);
const state = { sentiment: "All", query: "", reviewOnly: false, sort: "likes", page: 1 };
const pageSize = 6;
const percent = value => `${(value * 100).toFixed(1)}%`;
const date = value => new Date(value).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
let toastTimeout;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function initializeSummary() {
  $("#video-title").textContent = demo.video.title;
  $("#video-channel").textContent = demo.video.channel;
  $("#total-stat").textContent = summary.total;
  $("#coverage-stat").textContent = `of ${demo.video.fetched} fetched · ${percent(summary.total / demo.video.fetched)} coverage`;
  $("#nav-count").textContent = summary.total;
  $("#donut-total").textContent = summary.total;
  $("#review-count").textContent = summary.needsReview;
  for (const label of LABELS) {
    const key = label.toLowerCase();
    $(`#${key}-stat`).replaceChildren(document.createTextNode((summary.counts[label] / summary.total * 100).toFixed(1)), element("span", "", "%"));
    $(`#${key}-count`).textContent = `${summary.counts[label]} comments`;
    const row = element("div", "legend-row");
    row.append(element("i", `${key}-bg`), element("span", "", label),
               element("strong", "", percent(summary.counts[label] / summary.total)),
               element("small", "", summary.counts[label]));
    $("#legend").append(row);
  }
  const positiveEnd = summary.counts.Positive / summary.total * 100;
  const neutralEnd = positiveEnd + summary.counts.Neutral / summary.total * 100;
  $("#donut").style.background = `conic-gradient(var(--positive) 0% ${positiveEnd}%, var(--neutral) ${positiveEnd}% ${neutralEnd}%, var(--negative) ${neutralEnd}% 100%)`;
  $("#donut").setAttribute("aria-label", LABELS.map(label => `${summary.counts[label]} ${label.toLowerCase()}`).join(", ") + " comments");
  $("#fetched-count").textContent = demo.video.fetched;
  $("#analyzed-count").textContent = summary.total;
  $("#skipped-count").textContent = demo.video.skipped;
  $("#model-quality").textContent = `Held-out test accuracy: ${percent(demo.model.testAccuracy)} · Macro F1: ${percent(demo.model.testMacroF1)} · ${demo.model.testRows.toLocaleString()} test comments. These are dataset results, not the accuracy of this video.`;
}

function renderTrend() {
  const days = Array.from({ length: 7 }, (_, index) => `2026-10-0${index + 1}`);
  const counts = days.map(day => summarize(demo.comments.filter(comment => comment.publishedAt.startsWith(day))));
  const maximum = Math.ceil(Math.max(...counts.map(day => day.total)) / 3) * 3;
  const axis = element("div", "trend-axis");
  for (let step = 3; step >= 0; step--) axis.append(element("span", "", maximum / 3 * step));
  $("#trend").append(axis);
  counts.forEach((day, index) => {
    const column = element("div", "trend-day");
    const bar = element("div", "trend-bar");
    column.title = `Oct ${index + 1}: ${day.counts.Positive} positive, ${day.counts.Neutral} neutral, ${day.counts.Negative} negative`;
    for (const label of [...LABELS].reverse()) {
      const segment = element("span", `${label.toLowerCase()}-bg`);
      segment.style.height = `${day.counts[label] / maximum * 100}%`;
      bar.append(segment);
    }
    column.append(bar, element("span", "trend-day-label", `Oct ${index + 1}`));
    $("#trend").append(column);
  });
  $("#trend").setAttribute("aria-label", counts.map((day, index) =>
    `October ${index + 1}: ${day.counts.Positive} positive, ${day.counts.Neutral} neutral, ${day.counts.Negative} negative comments`).join(". "));
}

function renderFilters() {
  $(".filters").replaceChildren();
  for (const label of ["All", ...LABELS]) {
    const button = element("button", `filter${state.sentiment === label ? " active" : ""}`, label);
    button.type = "button";
    button.append(element("span", "", label === "All" ? summary.total : summary.counts[label]));
    button.setAttribute("aria-pressed", String(state.sentiment === label));
    button.addEventListener("click", () => { state.sentiment = label; state.page = 1; renderFilters(); renderComments(); });
    $(".filters").append(button);
  }
}

function openComment(comment) {
  $("#dialog-author").textContent = comment.author;
  $("#dialog-text").textContent = comment.text;
  $("#dialog-meta").replaceChildren(
    element("span", `sentiment-pill ${comment.sentiment.toLowerCase()}`, comment.sentiment),
    element("span", "", `${Math.round(comment.confidence * 100)}% model confidence`),
    element("span", "", `${comment.likes} likes`),
    element("span", "", date(comment.publishedAt)),
  );
  $("#comment-dialog").showModal();
}

function renderComments() {
  const filtered = filterComments(demo.comments, state);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  state.page = Math.min(state.page, pageCount);
  const start = (state.page - 1) * pageSize;
  $("#comment-rows").replaceChildren();
  for (const comment of filtered.slice(start, start + pageSize)) {
    const row = element("tr");
    const content = element("div", "comment-cell");
    const copy = element("div");
    copy.append(element("div", "comment-author", comment.author), element("p", "comment-text", comment.text));
    content.append(element("span", "avatar", comment.author.split(" ").map(part => part[0]).join("")), copy);
    const textCell = element("td");
    textCell.append(content);
    const sentimentCell = element("td");
    sentimentCell.append(element("span", `sentiment-pill ${comment.sentiment.toLowerCase()}`, comment.sentiment));
    const confidenceCell = element("td");
    const confidence = element("div", `confidence${comment.confidence < .6 ? " low" : ""}`);
    const track = element("span", "confidence-track");
    track.setAttribute("aria-hidden", "true");
    const fill = element("span");
    fill.style.width = `${comment.confidence * 100}%`;
    track.append(fill);
    confidence.append(element("span", "", `${Math.round(comment.confidence * 100)}%`), track);
    confidenceCell.append(confidence);
    const detailCell = element("td");
    const detail = element("button", "row-detail", "↗");
    detail.type = "button";
    detail.setAttribute("aria-label", `View comment by ${comment.author}`);
    detail.addEventListener("click", () => openComment(comment));
    detailCell.append(detail);
    row.append(textCell, sentimentCell, confidenceCell, element("td", "", comment.likes),
               element("td", "", date(comment.publishedAt)), detailCell);
    $("#comment-rows").append(row);
  }
  $("#empty-state").hidden = filtered.length !== 0;
  $("#result-badge").textContent = `${filtered.length} ${filtered.length === 1 ? "result" : "results"}`;
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
  const comments = filterComments(demo.comments, state);
  const blob = new Blob([commentsCsv(comments)], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = element("a");
  link.href = url;
  link.download = "comment-lens-demo-comments.csv";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  $("#toast").textContent = `Exported ${comments.length} sample comments with the current filters.`;
  $("#toast").hidden = false;
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => { $("#toast").hidden = true; }, 3500);
});
$("#video-form").addEventListener("submit", event => {
  event.preventDefault();
  const input = $("#video-url");
  const video = youtubeVideoUrl(input.value.trim());
  const feedback = $("#video-feedback");
  feedback.hidden = false;
  feedback.classList.toggle("error", !video);
  input.setAttribute("aria-invalid", String(!video));
  if (!video) { feedback.textContent = "Paste a valid HTTPS YouTube watch, Shorts, live, or youtu.be link."; return; }
  input.value = video;
  const url = new URL(location.href);
  url.searchParams.set("video", video);
  history.replaceState(null, "", url);
  feedback.textContent = `Selected: ${video}. The dashboard still shows sample comments; live results will be available when the backend is connected.`;
});

for (const link of document.querySelectorAll(".nav-link")) {
  link.addEventListener("click", () => {
    for (const sibling of document.querySelectorAll(".nav-link")) {
      sibling.classList.remove("active"); sibling.removeAttribute("aria-current");
    }
    link.classList.add("active"); link.setAttribute("aria-current", "page");
  });
}

const selected = youtubeVideoUrl(new URL(location.href).searchParams.get("video"));
if (selected) { $("#video-url").value = selected; $("#video-form").requestSubmit(); }
initializeSummary();
renderTrend();
renderFilters();
renderComments();
