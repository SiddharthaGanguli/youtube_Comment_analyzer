export const LABELS = ["Positive", "Neutral", "Negative"];

export function youtubeVideoUrl(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== "https:") return null;
  const host = url.hostname.toLowerCase();
  let id;
  if (host === "youtu.be") id = url.pathname.split("/")[1];
  else if (["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"].includes(host)) {
    if (url.pathname === "/watch") id = url.searchParams.get("v");
    else if (/^\/(shorts|live)\//.test(url.pathname)) id = url.pathname.split("/")[2];
  }
  return /^[a-zA-Z0-9_-]{11}$/.test(id || "") ? `https://www.youtube.com/watch?v=${id}` : null;
}

export function summarize(comments) {
  const counts = Object.fromEntries(LABELS.map(label => [label, 0]));
  for (const comment of comments) counts[comment.sentiment] += 1;
  return { counts, total: comments.length, needsReview: comments.filter(comment => comment.confidence < .6).length };
}

export function filterComments(comments, { sentiment = "All", query = "", reviewOnly = false, sort = "likes" } = {}) {
  const search = query.trim().toLocaleLowerCase();
  const result = comments.filter(comment =>
    (sentiment === "All" || comment.sentiment === sentiment) &&
    (!reviewOnly || comment.confidence < .6) &&
    `${comment.text} ${comment.author}`.toLocaleLowerCase().includes(search));
  return result.sort((a, b) => sort === "recent" ? b.publishedAt.localeCompare(a.publishedAt) :
    sort === "confidence" ? a.confidence - b.confidence : b.likes - a.likes);
}

export function commentsCsv(comments) {
  const escape = value => {
    let text = String(value);
    // A comment must remain text when a spreadsheet opens the exported CSV.
    if (/^[\s]*[=+\-@\t\r]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  return "\uFEFF" + [
    ["Author", "Comment", "Sentiment", "Model confidence", "Likes", "Published at"],
    ...comments.map(comment => [comment.author, comment.text, comment.sentiment,
                               comment.confidence, comment.likes, comment.publishedAt]),
  ].map(row => row.map(escape).join(",")).join("\r\n");
}
