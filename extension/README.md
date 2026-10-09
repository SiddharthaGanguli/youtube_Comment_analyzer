# Comment Lens extension preview

The V1 design uses a small popup and a dashboard in a full tab. It is a
Manifest V3 extension with plain HTML, CSS and JavaScript, so there is no build
step and the code is easy to follow.

The preview has a video-link input, sentiment totals, coverage, a daily chart,
search, sentiment/confidence filters, sorting, pagination, comment details,
and CSV export. All dashboard numbers come from the same 24 handwritten sample
comments in `demo-data.mjs`. The design labels the data as a demo throughout.
Selecting a real YouTube URL does not fetch or analyze its comments yet.

## Try it in Chrome

1. Open `chrome://extensions` and enable **Developer mode**.
2. Choose **Load unpacked** and select this `extension` folder.
3. Pin **Comment Lens**, open a YouTube video, and click the extension.
4. Choose **Open dashboard** or **Explore the demo**.

The popup reads the current tab's title and URL only when invoked, using
`activeTab`. The preview makes no external requests. JavaScript is packaged
locally, following Chrome's [Manifest V3 format](https://developer.chrome.com/docs/extensions/reference/manifest),
[activeTab guidance](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab),
and [extension content security policy](https://developer.chrome.com/docs/extensions/reference/manifest/content-security-policy).

You can also preview the pages without installing the extension:

```powershell
# From the repository root:
.\.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1 --directory extension
```

Open `http://127.0.0.1:8765/popup.html` or
`http://127.0.0.1:8765/dashboard.html`. Browser previews support URL entry and
dashboard interactions; detecting the active YouTube tab requires the extension.

## Where to make changes

| File | What it does |
| --- | --- |
| `manifest.json` | Extension name, popup, and permissions |
| `popup.html`, `popup.mjs` | Current-video detection and dashboard launch |
| `dashboard.html`, `dashboard.mjs` | Dashboard sections and interactions |
| `styles.css` | Shared layout, colors, typography, and responsive rules |
| `demo-data.mjs` | Handwritten comment sample and current baseline metrics |
| `lib.mjs` | URL parsing, filtering, summary counts, and CSV formatting |

Run the small behavior checks with Node.js:

```powershell
node --test extension/tests/lib.test.mjs
```

The checks cover URL validation, combined filters, consistent counts, and safe
CSV quoting. The design has also been checked in headless Chrome for navigation,
search, filters, pagination, export, comment details, and mobile overflow.

## Finalized design

The popup selects the current video or opens the demo. The dashboard keeps the
video and totals at the top, followed by sentiment/daily charts, comments needing
review, the searchable comment table, and coverage/model details. The quiet
sidebar links to these sections. Below 700px the charts stack; the comment table
scrolls inside its own container so every column remains available.

The review preserved this layout and corrected keyboard focus when a sentiment
filter is selected. Demo status, skipped counts, and the meaning of confidence
stay visible. Browser checks cover 1440px desktop and 390px mobile layouts,
keyboard filtering, dialog close/focus, search, exports, pagination, and URL
feedback. Automated accessibility checks cover the dashboard and popup; they
complement the visual and keyboard review.

## Backend handoff

The serving [system design](../docs/system-design.md),
[HTTP/result contract](../docs/backend-contract.yaml), and
[implementation plan](../docs/backend-plan.md) specify sign-in, job polling,
private result downloads, latest top-level sampling and launch limits.
They are specifications; the preview is not connected to a backend yet.

Keep `demo-data.mjs` as the preview fixture when the backend is added. A real
analysis response should supply the video's metadata, fetched/analyzed/skipped
counts, sample period, model version, and comments. Each comment needs an ID,
author, text, sentiment, confidence, likes, and publish time. The charts should
aggregate the returned comments, and exports should use the current filters.

The shared [prediction service](../docs/prediction.md) is ready for the backend.
It accepts raw text and returns ordered results, three class scores, review
flags, skip reasons, coverage, and a model fingerprint. Join those results to
the fetched comment metadata using their input indices. Keep unknown-vocabulary
skips visible alongside empty-comment skips; compute chart percentages from
analyzed comments only.

The backend will validate the URL, fetch comments, call the prediction service,
and return video/comment metadata with predictions. The extension will need loading,
progress, empty-video and failure states before live analysis is enabled.
YouTube/AWS credentials belong in that backend. They never belong in the
extension bundle. The preview still uses the demo fixture. HTTP endpoints,
YouTube fetching, connecting the extension, and AWS deployment remain future work.
