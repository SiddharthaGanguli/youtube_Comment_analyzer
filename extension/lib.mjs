export const LABELS = ["Positive", "Neutral", "Negative"];

export function youtubeVideoUrl(value) {
  if (typeof value !== "string" || /[\s\x00-\x1f\x7f]/.test(value)) return null;
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== "https:" || url.username || url.password || url.port) return null;
  const host = url.hostname.toLowerCase();
  let id;
  if (host === "youtu.be") id = /^\/([A-Za-z0-9_-]{11})\/?$/.exec(url.pathname)?.[1];
  else if (["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"].includes(host)) {
    if (url.pathname === "/watch" && url.searchParams.getAll("v").length === 1) id = url.searchParams.get("v");
    else id = /^\/(?:shorts|live)\/([A-Za-z0-9_-]{11})\/?$/.exec(url.pathname)?.[1];
  }
  return /^[a-zA-Z0-9_-]{11}$/.test(id || "") ? `https://www.youtube.com/watch?v=${id}` : null;
}

export function summarize(comments) {
  const counts = Object.fromEntries(LABELS.map(label => [label, 0]));
  const analyzed = comments.filter(comment => LABELS.includes(comment.sentiment) && comment.status !== "skipped");
  for (const comment of analyzed) counts[comment.sentiment] += 1;
  return { counts, total: analyzed.length, needsReview: analyzed.filter(isReview).length };
}

export function isReview(comment) {
  return comment.status !== "skipped" && Number.isFinite(comment.confidence) && (comment.needsReview ?? comment.confidence < .6);
}

export function filterComments(comments, { sentiment = "All", query = "", reviewOnly = false, sort = "likes" } = {}) {
  const search = query.trim().toLocaleLowerCase();
  const result = comments.filter(comment =>
    (sentiment === "All" || comment.sentiment === sentiment) &&
    (!reviewOnly || isReview(comment)) &&
    `${comment.text ?? ""} ${comment.author ?? ""}`.toLocaleLowerCase().includes(search));
  return result.sort((a, b) => sort === "recent" ? b.publishedAt.localeCompare(a.publishedAt) :
    sort === "confidence" ? (a.confidence ?? Infinity) - (b.confidence ?? Infinity) : b.likes - a.likes);
}

export function commentsCsv(comments) {
  const escape = value => {
    let text = String(value);
    // A comment must remain text when a spreadsheet opens the exported CSV.
    if (/^[\s]*[=+\-@\t\r]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  return "\uFEFF" + [
    ["Author", "Comment", "Sentiment", "Model confidence", "Likes", "Published at", "Status", "Skip reason"],
    ...comments.map(comment => [comment.author ?? "", comment.text ?? "", comment.sentiment ?? "",
                               comment.confidence ?? "", comment.likes, comment.publishedAt,
                               comment.status ?? "analyzed", comment.skippedReason ?? ""]),
  ].map(row => row.map(escape).join(",")).join("\r\n");
}

// At most seven UTC buckets covering the complete sample, including sparse dates.
export function trendBuckets(comments) {
  if (!comments.length) return [];
  const days = comments.map(row => Math.floor(Date.parse(row.publishedAt) / 86400000));
  const first = Math.min(...days), last = Math.max(...days);
  const width = Math.max(1, Math.ceil((last - first + 1) / 7));
  return Array.from({length: Math.ceil((last - first + 1) / width)}, (_, index) => {
    const start = first + index * width, end = Math.min(last, start + width - 1);
    return {start: new Date(start * 86400000).toISOString(), end: new Date(end * 86400000).toISOString(),
      ...summarize(comments.filter((row, rowIndex) => days[rowIndex] >= start && days[rowIndex] <= end))};
  });
}
