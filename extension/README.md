# Comment Lens extension and demo

The V1 design uses a small popup and a dashboard in a full tab. It is a
Manifest V3 extension with plain HTML, CSS and JavaScript. The default source
works as a demo; the bundle builder adds public deployment settings and exact
permissions for a configured extension. The AWS development backend and website
connection are deployed; the extension still needs its own registered Chrome ID.

Try the [sample comments demo](https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html?demo=1#comments)
without signing in. It opens 24 labeled, handwritten comments, with filters,
search, details and CSV export. The normal
[analysis workspace](https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html)
starts empty and offers account sign-in. Demo comments are not results for a
video link you paste.

On the homepage, [Login](https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html?auth=login#account)
and [Signup](https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html?auth=signup#account)
open the dashboard's account section. Accept the Terms of Service and Privacy
Policy, then continue to Amazon Cognito; signup includes email confirmation.
The dashboard's Comment Lens logo returns to the homepage.
Website sign-in survives reloads and Home/Dashboard navigation in the same tab
until its short access-token expiry or **Sign out**, including while viewing
demo data. Browsing does not extend that expiry.

The preview has a video-link input, sentiment totals, coverage, a daily chart,
search, sentiment/confidence filters, sorting, pagination, comment details,
and CSV export. Demo numbers come from the same 24 handwritten sample comments
in `demo-data.mjs`, and the interface labels them as demo data.
Selecting a real YouTube URL in demo mode does not fetch or analyze its comments.
Step 7 implements the account/API connection alongside this preview. Its
behavior is checked with offline responses and DOM fixtures. A generated
connected extension bundle still needs its assigned public settings and real
Chrome validation. Website sign-in and authenticated submission to the
disabled-live gate have been verified; live fetching awaits YouTube approval.

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

## Build a demo extension

From the repository root, with Python and Node.js available:

```powershell
.\.venv\Scripts\python.exe scripts/build_extension_bundle.py
```

Load `artifacts/extension` through **Load unpacked**. This generated demo
has only `activeTab` permission and no configured account/API connection.
The builder copies an explicit runtime-file allowlist and excludes tests,
backend code, datasets and credentials. It refuses linked paths and unexpected
files in an existing output directory.

## Build a connected extension after deployment

Use the assigned deployment outputs in a flat public JSON file, then build:

```powershell
.\.venv\Scripts\python.exe scripts/build_extension_bundle.py --settings public-extension-settings.json
```

The JSON uses these fields; it contains public metadata, never tokens, client
secrets, AWS credentials, a YouTube key or its secret value:

| Field | Value |
| --- | --- |
| `enabled` | `true` for the configured connection; the checked-in default is `false` |
| `apiBaseUrl` | Assigned API HTTPS origin, with a stage path if needed |
| `cognitoDomain` | Assigned Cognito hosted-login HTTPS origin |
| `clientId` | Assigned Cognito public-client ID |
| `extensionId` | Actual 32-character ID shown by Chrome for this package |
| `resultBaseUrl` | Assigned virtual-hosted S3 bucket HTTPS URL plus `serving/development` or `serving/production` prefix |
| `pollScheduleSeconds` | Optional `[2, 5, 10]`; this is also the default |
| `maxComments` | Optional integer from 1 to 1,000; defaults to 1,000 |

The deployed development website uses `../config/web-client.public.json`.
That file has a website callback rather than an extension ID, so it cannot be
used unchanged for an extension bundle. Use the real assigned AWS values and
register the extension callback described below. Unknown fields, URL credentials,
query strings and broader or unrelated result destinations are rejected.

First load the generated demo directory and note its extension ID. Keep the
package in that same directory when rebuilding. Its configured ID must match
Chrome and the Cognito callback
`https://<extension-id>.chromiumapp.org/cognito`. Moving an unpacked package can
change its ID. The enabled manifest adds `identity`, `storage` and exactly the
three configured API, Cognito and S3 host origins. Those origins must be
distinct; there is no permission for every website.

The builder creates files only. It does not deploy AWS, enable YouTube fetching
or treat audit submission as approval. Default source settings stay disabled,
while the public Pages dashboard uses its separate configured website connection.
Use `dashboard.html?demo=1#comments` to open its sample comments directly.

## Try the connected states without AWS

Build a separate developer fixture preview:

```powershell
.\.venv\Scripts\python.exe scripts/preview_extension_fixture.py
.\.venv\Scripts\python.exe -m http.server 8766 --bind 127.0.0.1 --directory artifacts/extension-fixture
```

Open `http://127.0.0.1:8766/dashboard.html?scenario=success`. Accept the test
policy checkbox, choose **Use fixture account**, then **Analyze comments**.
The banner identifies this as a developer fixture test. It uses synthetic
responses and makes no AWS, YouTube or real sign-in request.

The scenario selector covers success, partial results, empty results,
all-skipped comments, quota exhaustion, a busy service, failure and expiration.
Ordinary 2/5-second polling advances the queued/fetching examples; the expiration
scenario clears after 15 seconds. Filters, details, CSV, retry and removal can
be exercised against the resulting UI. Pass `--output` to the preview builder
to choose another empty or builder-owned directory. It does not start a server.

## Published website and sample demo

The website is published on GitHub Pages. The standard dashboard opens the
connected workspace with no comments until a verified result is available.
The explicit sample-demo link opens the handwritten comments without sign-in:

| Page | Public URL |
| --- | --- |
| Homepage / Primary Access | https://siddharthaganguli.github.io/youtube_Comment_analyzer/ |
| Analysis workspace | https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html |
| Sample comments demo | https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html?demo=1#comments |
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
Demo mode uses handwritten samples. The AWS development backend is deployed and
website sign-in is verified, while accepted analytics-use approval and live
comment analysis remain pending. Changing the input URL in demo mode never
changes its sample into actual video results.

[Publish Comment Lens website](../.github/workflows/pages.yaml) runs on matching pushes to
`main` or manual dispatch. It checks the JavaScript behavior suites, runs
`scripts/build_pages_site.py`, uploads `_site`, and deploys through the
`github-pages` environment. The builder publishes only its explicit allowlist:
the homepage, dashboard and legal HTML, their CSS, dashboard modules, the
official logo, and `.nojekyll`. Configured builds also include the website
authentication/client modules and validated public connection settings. It
excludes the extension manifest/popup and extension-only session modules,
private backend configuration, credentials, datasets, model artifacts and
fixture preview modules.

## Privacy policy

The popup and dashboard link to `privacy.html`. It describes demo mode separately
from configured connected builds, including sign-in storage, YouTube data use,
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
Pages and its hosting logs. The connected extension now has policy consent and
session controls; its native Chrome account flow still needs verification.
Website sign-in has been verified against the development deployment.
The developer reported submitting the audit on 15 October 2026;
an accepted analytics-use decision and case reference have not been supplied.

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
Public HTTPS hosting is complete. A page-content preview does not replace a
genuine browser-window screenshot with its address bar. Retain the actual
submitted evidence and correspondence with the audit record.

## Where to make changes

| File | What it does |
| --- | --- |
| `manifest.json` | Extension name, popup, and permissions |
| `popup.html`, `popup.mjs` | Current-video detection and dashboard launch |
| `dashboard.html`, `dashboard-app.mjs`, `dashboard.mjs` | Dashboard rendering, explicit demo mode, and website/extension connection bootstrap |
| `styles.css` | Shared layout, colors, typography, and responsive rules |
| `privacy.html`, `terms.html`, `privacy.css` | Legal pages and their shared layout |
| `index.html`, `home.css`, `assets/` | Demo homepage and official YouTube branding |
| `demo-data.mjs` | Handwritten comment sample and current baseline metrics |
| `lib.mjs` | URL parsing, filtering, summary counts, and CSV formatting |
| `client-config.mjs`, `client-settings.mjs` | Validated public settings and disabled source defaults |
| `auth-pkce.mjs`, `auth-session.mjs` | One-use PKCE flow and trusted access-token session |
| `web-client-config.mjs`, `web-client-settings.mjs`, `web-auth-session.mjs` | Separate website connection settings and browser sign-in |
| `analysis-client.mjs`, `result-model.mjs` | Authenticated API calls and verified result/view data |
| `job-controller.mjs` | Submission, polling, retry, expiry and removal lifecycle |

Run the behavior and DOM-fixture checks from the repository root:

```powershell
npm ci
npm test
.\.venv\Scripts\python.exe -m unittest tests.test_extension_bundle -v
```

The checks cover public settings, PKCE/session behavior, ownership-bound
downloads, counts, controller transitions, cancellation, local filters and safe
CSV quoting. jsdom is a development test dependency; the extension's runtime
still uses packaged JavaScript without a third-party frontend framework.

## Finalized design

The popup selects the current video or opens the demo. The dashboard keeps the
video and totals at the top, followed by sentiment/daily charts, comments needing
review, the searchable comment table, and coverage/model details. The quiet
sidebar links to these sections. Below 700px the charts stack; the comment table
scrolls inside its own container so every column remains available.

The earlier demo review preserved this layout and corrected keyboard focus
when a sentiment filter is selected. Demo status, skipped counts, and the
meaning of confidence stay visible. Those earlier demo checks do not establish
that the new connected screens work in native Chrome. The current stage's
native Chrome verification was blocked by the browser control's URL safety
check. Connected extension layout, keyboard/accessibility, native extension
sign-in and live-analysis checks remain pending; DOM fixtures do not replace them.

## Backend handoff

The serving [system design](../docs/system-design.md),
[HTTP/result contract](../docs/backend-contract.yaml), and
[implementation plan](../docs/backend-plan.md) specify sign-in, job polling,
private result downloads, latest top-level sampling and launch limits.
The backend routes and extension connection are implemented. Real connection
requires a generated enabled bundle and the assigned deployment outputs.

`demo-data.mjs` remains the labeled preview. The connected dashboard instead
uses a validated result with the video's metadata, counts, sample period,
model version and comments. Skipped rows and missing authors have explicit
display values. Charts use analyzed comments; coverage and skipped counts stay
visible. Filters, search, charts and CSV share that one in-memory result.

The shared [prediction service](../docs/prediction.md) is ready for the backend.
It accepts raw text and returns ordered results, three class scores, review
flags, skip reasons, coverage, and a model fingerprint. Join those results to
the fetched comment metadata using their input indices. Keep unknown-vocabulary
skips visible alongside empty-comment skips; compute chart percentages from
analyzed comments only.

Before account access or submission, the connected UI requires the policy and
terms checkbox. Sign-in starts only after a click. `AuthSession` uses S256 and
one-use state, and stores only the access token, expiry and a nonsecret revision
in `chrome.storage.session` restricted to trusted extension contexts. It does
not keep an ID token or refresh token. Access-token expiry requires sign-in
again. Signing out clears this extension session and invalidates pending work
across its windows; it does not perform global Cognito logout or close an account.

The controller shows queued/fetching/inference/saving progress and distinct
partial, empty, all-skipped, quota, busy, error, expired and deleted states.
Polling follows 2/5/10-second delays with jitter, honors server retry delays,
and pauses while hidden. A retry of an uncertain submission reuses its UUID
key while that submission remains in the open controller. Result downloads
verify their byte count, SHA-256 and association with the owned status; one
expired/403 download link can be renewed without resubmitting the job.
Removal deletes the owned history reference, while shared work/results follow
the backend's retention rules. Sign-out, account changes and expiry clear
displayed account results and stop stale requests from repainting the UI.

YouTube/AWS credentials stay in the backend. The AWS development service and
website Cognito sign-in are deployed and verified. The approved live sample,
native extension sign-in and load testing remain pending. See the
[connection walkthrough](../docs/serving-configuration.md#extension-connection-and-generated-bundles)
for the implementation order and remaining checks.
