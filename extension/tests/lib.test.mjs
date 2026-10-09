import test from "node:test";
import assert from "node:assert/strict";
import { youtubeVideoUrl, summarize, filterComments, commentsCsv } from "../lib.mjs";
import { demo } from "../demo-data.mjs";

test("YouTube URL parsing supports watch, Shorts, live and short links without trusting other hosts", () => {
  for (const value of ["https://youtu.be/abcdefghijk?t=10", "https://www.youtube.com/watch?v=abcdefghijk&list=other",
    "https://m.youtube.com/shorts/abcdefghijk", "https://youtube.com/live/abcdefghijk"]) {
    assert.equal(youtubeVideoUrl(value), "https://www.youtube.com/watch?v=abcdefghijk");
  }
  for (const value of [null, "bad-url", "https://youtube.com.evil.test/watch?v=abcdefghijk",
    "https://www.youtube.com/@channel", "javascript:alert(1)", "http://youtu.be/abcdefghijk",
    "https://youtu.be/short"]) assert.equal(youtubeVideoUrl(value), null);
});

test("The demo totals, coverage and review count remain consistent", () => {
  const summary = summarize(demo.comments);
  assert.deepEqual(summary.counts, { Positive: 14, Neutral: 6, Negative: 4 });
  assert.equal(summary.total + demo.video.skipped, demo.video.fetched);
  assert.equal(summary.needsReview, 5);
  assert.equal(new Set(demo.comments.map(comment => comment.id)).size, summary.total);
});

test("Search, sentiment and confidence filters combine and sorting never mutates the source", () => {
  const original = demo.comments.map(comment => comment.id);
  const result = filterComments(demo.comments, { sentiment: "Positive", query: "not bad", reviewOnly: true });
  assert.equal(result.length, 1);
  assert.equal(result[0].author, "Riya P.");
  const sorted = filterComments(demo.comments, { sort: "confidence" });
  assert.equal(sorted[0].confidence, .52);
  assert.deepEqual(demo.comments.map(comment => comment.id), original);
  assert.equal(filterComments(demo.comments, { query: "does not exist" }).length, 0);
});

test("CSV export preserves quoted multiline text and prevents spreadsheet formulas", () => {
  const text = commentsCsv([{ author: "=SUM(1,1)", text: 'hello, "world"\n=not a formula',
                             sentiment: "Neutral", confidence: .52, likes: 0, publishedAt: "2026-10-01" }]);
  assert.ok(text.startsWith("\uFEFF"));
  assert.ok(text.includes('"\'=SUM(1,1)"'));
  assert.ok(text.includes('"hello, ""world""\n=not a formula"'));
  assert.equal(text.split("\r\n").length, 2);
});
