import { youtubeVideoUrl } from "./lib.mjs";

const input = document.querySelector("#video-url");
const error = document.querySelector("#url-error");
const current = document.querySelector("#current-video");

async function openDashboard(video = null) {
  const path = new URL("dashboard.html", location.href);
  if (video) path.searchParams.set("video", video);
  if (globalThis.chrome?.tabs?.create && location.protocol === "chrome-extension:") {
    await chrome.tabs.create({ url: path.href });
    window.close();
  } else window.location.href = path.href;
}

document.querySelector("#popup-form").addEventListener("submit", async event => {
  event.preventDefault();
  const value = input.value.trim();
  const video = value ? youtubeVideoUrl(value) : null;
  if (value && !video) {
    error.textContent = "Use a valid YouTube watch, Shorts, live, or youtu.be link.";
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
    return;
  }
  error.hidden = true;
  input.removeAttribute("aria-invalid");
  try { await openDashboard(video); }
  catch { error.textContent = "The dashboard could not open. Please try again."; error.hidden = false; }
});
document.querySelector("#try-demo").addEventListener("click", () => openDashboard());
document.querySelector("#brand-link").addEventListener("click", event => {
  event.preventDefault();
  openDashboard();
});
input.addEventListener("input", () => { error.hidden = true; input.removeAttribute("aria-invalid"); });

if (globalThis.chrome?.tabs?.query && location.protocol === "chrome-extension:") {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const video = youtubeVideoUrl(tab?.url);
    if (video) {
      input.value = video;
      current.textContent = (tab.title || "YouTube video").replace(/\s*- YouTube$/, "");
    }
  } catch { current.textContent = "Paste a YouTube video link to open its dashboard preview."; }
}
