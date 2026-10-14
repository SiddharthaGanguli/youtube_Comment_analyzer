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
`activeTab`. The preview makes no YouTube Data API or Comment Lens backend
calls. JavaScript is packaged locally, following Chrome's [Manifest V3 format](https://developer.chrome.com/docs/extensions/reference/manifest),
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

## Published demo

The static demo is published on GitHub Pages. These HTTPS pages and their assets
were verified on 14 October 2026:

| Page | Public URL |
| --- | --- |
| Homepage / Primary Access | https://siddharthaganguli.github.io/youtube_Comment_analyzer/ |
| Dashboard | https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html |
| Privacy Policy | https://siddharthaganguli.github.io/youtube_Comment_analyzer/privacy.html |
| Terms of Service | https://siddharthaganguli.github.io/youtube_Comment_analyzer/terms.html |

Initial publication passed the [Pages deployment](https://github.com/SiddharthaGanguli/youtube_Comment_analyzer/actions/runs/38549129835)
and [Pipeline checks](https://github.com/SiddharthaGanguli/youtube_Comment_analyzer/actions/runs/38549129822)
for commit `dbb7e5b`. Browser checks also covered legal-page navigation and
dashboard filtering/search on the hosted demo.

GitHub receives requests for these pages and assets and logs visitor IP addresses
for security. Opening or reloading a dashboard URL containing a selected video
link also sends that URL to GitHub. See the published privacy policy and
[GitHub's Privacy Statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement).
The dashboard still uses handwritten samples; live analysis, analytics-use
approval and AWS backend deployment remain pending.

[Publish demo pages](../.github/workflows/pages.yaml) runs on matching pushes to
`main` or manual dispatch. It checks the two JavaScript behavior suites, runs
`scripts/build_pages_site.py`, uploads `_site`, and deploys through the
`github-pages` environment. The builder publishes only its explicit allowlist:
the homepage, dashboard and legal HTML, their CSS, the dashboard/demo/library
JavaScript, the official logo, and `.nojekyll`. It excludes the extension
manifest/popup, backend, configuration, credentials, datasets and model artifacts.

## Privacy policy

The popup and dashboard link to `privacy.html`. It describes the current static
preview separately from the planned live service, including YouTube data use,
AWS processing, cache sharing, retention and email deletion requests. The public
privacy contact is `siddharthaganguli0093@gmail.com`, supplied by the developer.
The page uses local CSS and no script, remote fonts or embedded media.

The upload PDF is `../output/pdf/comment-lens-privacy-policy.pdf`. Its text and
links are exported from the HTML page, so edit the page and rebuild the PDF with:

    uv run scripts/export_privacy_policy.py

Run that command from the repository root. The script declares its own document
dependency; it does not add packages to the application's runtime requirements.
Open `http://127.0.0.1:8765/privacy.html` to review the page locally.

The PDF is prepared for the audit form's first evidence box. Use the published
Privacy Policy URL above in the form's URL field. The policy identifies GitHub
Pages and its hosting logs; live consent/account controls still need to be
completed before live features launch. The demo homepage and branding are
described below; final browser-window evidence still needs its address bar.

## Terms of Service

The popup, dashboard and privacy page link to `terms.html`. It identifies the
independent developer, includes the YouTube terms agreement, and explains demo
status, model limitations, responsible use, content rights, exports, live
limits, expiration and deletion. It uses the privacy page's local stylesheet.

Upload `../output/pdf/comment-lens-terms-of-service.pdf` in the audit form's
**Terms of Service Documentation** box. Edit the HTML and rebuild it with:

    uv run scripts/export_terms_of_service.py

Run from the repository root. This uses the same document renderer as the
privacy export. Both exports preserve active external YouTube/Google/contact
links and resolve relative app-page links to the published GitHub Pages URLs.

Review locally at `http://127.0.0.1:8765/terms.html` or use the published Terms
of Service URL above. The PDF files are upload evidence documents; use the
published HTML URLs for the form's URL fields.

## Homepage and screenshot evidence

`index.html` is the demo homepage, linked from the app and legal pages.
It displays the Privacy Policy link in its header, a second policy link in
the privacy card, the development/demo notice, and an official YouTube logo
in a separate planned-integration card. The logo is unchanged, keeps its
proportions and links to YouTube. Its provenance is in `assets/README.md`.
The extension manifest's product name is now **Comment Lens**.

Open `http://127.0.0.1:8765/index.html` with the preview server running.
`../output/evidence/homepage-preview.jpg` captures the published homepage content;
it has no browser address bar and is a preview, not final audit evidence.

For the **Homepage Screenshot** evidence box, capture the actual Chrome window
including its address bar with Windows Snipping Tool (Win+Shift+S, Window mode).
Keep the Privacy Policy link, YouTube branding and demo notice visible. Use
PNG or JPEG, at least 1280x720 pixels, and a file below 10 MB. Open the published
Homepage URL above so the real HTTPS address appears in the capture. Do not draw
a browser bar.

The form's screenshot quality requirements are documented in the
[official audit form](https://support.google.com/youtube/contact/yt_api_form?hl=en).
Public HTTPS hosting is complete. The genuine Chrome window capture with its
address bar remains a manual step; the page-content preview does not replace it.

## Where to make changes

| File | What it does |
| --- | --- |
| `manifest.json` | Extension name, popup, and permissions |
| `popup.html`, `popup.mjs` | Current-video detection and dashboard launch |
| `dashboard.html`, `dashboard.mjs` | Dashboard sections and interactions |
| `styles.css` | Shared layout, colors, typography, and responsive rules |
| `privacy.html`, `terms.html`, `privacy.css` | Legal pages and their shared layout |
| `index.html`, `home.css`, `assets/` | Demo homepage and official YouTube branding |
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
extension bundle. The preview still uses the demo fixture. Live YouTube
fetching, connecting the extension, and AWS deployment remain future
work. HTTP routes and the fixture worker are implemented and tested separately;
this preview still makes no backend requests.

`auth-pkce.mjs` prepares the later sign-in flow: fresh state/verifier, mandatory
S256, an exact Chromium callback and a one-use token-exchange descriptor. It
performs no request and stores no token. The UI does not call it yet; `identity`
permissions, restricted session storage and deployed public settings are added
when connecting the extension. Run both module suites with
`node --test extension/tests/lib.test.mjs extension/tests/auth-pkce.test.mjs`.
