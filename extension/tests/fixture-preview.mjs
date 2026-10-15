// Developer-only entry point. The fixture builder copies this beside the app.
// These relative imports target that generated directory, not extension/tests/.
import {configureService} from "./dashboard-app.mjs";
import {JobController} from "./job-controller.mjs";
import {validateAnalysis, validateResult, validateModelDetails, resultToView} from "./result-model.mjs";
import {FIXTURE_SCENARIOS, FIXTURE_VIDEO_URL, makeFixtureResult, makeFixtureAnalysis,
  makeFixtureModelDetails} from "./fixture-data.mjs";

if (!['127.0.0.1', 'localhost', '[::1]', '::1'].includes(location.hostname)
    || !['http:', 'https:'].includes(location.protocol)) {
  throw new Error("The developer fixture preview must run on a local HTTP server");
}

const selected = new URL(location.href).searchParams.get("scenario") || "success";
const scenario = FIXTURE_SCENARIOS.includes(selected) ? selected : "success";
const picker = document.querySelector("#fixture-scenario");
for (const name of FIXTURE_SCENARIOS) {
  const option = document.createElement("option"); option.value = name; option.textContent = name;
  option.selected = name === scenario; picker.append(option);
}
picker.addEventListener("change", () => {
  const url = new URL(location.href); url.searchParams.set("scenario", picker.value); location.href = url.href;
});
document.querySelector("#fixture-description").textContent =
  `Scenario: ${scenario}. All accounts, comments, states and evaluation numbers are synthetic.`
  + (scenario === "expired" ? " The result expires 15 seconds after submission." : "");
const warning = document.querySelector("#fixture-warning");
const positionSidebar = () => {document.querySelector(".sidebar").style.top = `${warning.getBoundingClientRect().height}px`;};
positionSidebar(); window.addEventListener("resize", positionSidebar);

function error(code, {retryAfterSeconds = null, resetAt = null} = {}) {
  return Object.assign(new Error("Synthetic fixture failure"), {code, retryable: true, retryAfterSeconds, resetAt});
}

function wait(signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {reject(error("REQUEST_ABORTED")); return;}
    const abort = () => {clearTimeout(timer); signal.removeEventListener("abort", abort); reject(error("REQUEST_ABORTED"));};
    const timer = setTimeout(() => {signal?.removeEventListener("abort", abort); resolve();}, 180);
    signal?.addEventListener("abort", abort, {once: true});
  });
}

const auth = {
  signedIn: false,
  async initialize() {return this.signedIn;},
  async signIn() {this.signedIn = true; return true;},
  async signOut() {this.signedIn = false;},
  async getAccessToken() {if (!this.signedIn) throw error("AUTH_REQUIRED"); return "synthetic-fixture-session-only";},
  dispose() {},
};
const jobs = new Map(), submissions = new Map();
let latestResult;
const api = {
  async submit(videoUrl, key, {signal} = {}) {
    await wait(signal);
    await auth.getAccessToken();
    if (scenario === "quota") throw error("YOUTUBE_QUOTA_EXHAUSTED", {
      retryAfterSeconds: 60, resetAt: new Date(Date.now() + 60_000).toISOString()});
    if (scenario === "busy") throw error("SYSTEM_BUSY", {retryAfterSeconds: 10});
    let job = submissions.get(key);
    if (!job) {
      const result = makeFixtureResult({scenario, videoId: new URL(videoUrl).searchParams.get("v")});
      job = {id: crypto.randomUUID(), result, stage: 0};
      latestResult = result; submissions.set(key, job); jobs.set(job.id, job);
    }
    return validateAnalysis(makeFixtureAnalysis({state: "QUEUED", result: job.result, analysisId: job.id}));
  },
  async status(id, {signal} = {}) {
    await wait(signal); await auth.getAccessToken();
    const job = jobs.get(id); if (!job) throw error("NOT_FOUND");
    const state = ++job.stage === 1 ? "FETCHING" : scenario === "failed" ? "FAILED" : job.result.state;
    return validateAnalysis(makeFixtureAnalysis({state, result: job.result, analysisId: job.id}));
  },
  async download(id, {signal, analysis} = {}) {
    await wait(signal); await auth.getAccessToken();
    const job = jobs.get(id); if (!job) throw error("NOT_FOUND");
    return validateResult(structuredClone(job.result), analysis);
  },
  async delete(id, {signal} = {}) {await wait(signal); await auth.getAccessToken(); jobs.delete(id);},
  async currentModel({signal} = {}) {await wait(signal); return validateModelDetails(makeFixtureModelDetails(latestResult));},
};

const controller = new JobController({api, auth});
configureService(controller, resultToView);
document.querySelector("#sign-in").textContent = "Use fixture account";
document.querySelector("#use-service").textContent = "Open fixture workspace";
document.querySelector("#video-url").value = FIXTURE_VIDEO_URL;
document.title = `FIXTURE TEST (${scenario}) | Comment Lens`;
await controller.initialize();
document.querySelector("#use-service").click();
window.addEventListener("pagehide", () => controller.dispose(), {once: true});
