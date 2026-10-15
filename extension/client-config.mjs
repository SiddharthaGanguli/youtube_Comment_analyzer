// Public deployment metadata only. Tokens and API keys never belong here.
function invalid() { throw new Error("The extension connection settings are invalid"); }
function object(value) { return value && typeof value === "object" && !Array.isArray(value); }

function https(value) {
  if (typeof value !== "string" || value.length > 2048 || /[\s\x00-\x1f\x7f]/.test(value)) invalid();
  let url;
  try { url = new URL(value); } catch { invalid(); }
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password || url.port || url.search || url.hash
      || !/^[a-z0-9.-]+$/.test(url.hostname) || url.hostname === "localhost"
      || /^\d+(?:\.\d+){3}$/.test(url.hostname) || value.includes("%")
      || value.includes("\\") || /(?:^|\/)\.{1,2}(?:\/|$)/.test(value)) invalid();
  return url;
}

export function validateClientConfig(raw = {enabled: false}) {
  if (!object(raw) || typeof raw.enabled !== "boolean") invalid();
  const allowed = new Set(["enabled", "apiBaseUrl", "cognitoDomain", "clientId", "extensionId", "resultBaseUrl",
    "pollScheduleSeconds", "maxComments"]);
  if (Object.keys(raw).some(key => !allowed.has(key))) invalid();
  if (!raw.enabled) {
    if (Object.keys(raw).length !== 1) invalid();
    return Object.freeze({enabled: false});
  }
  const api = https(raw.apiBaseUrl), cognito = https(raw.cognitoDomain), result = https(raw.resultBaseUrl);
  if (!/^\/(?:[A-Za-z0-9_-]+\/?)*$/.test(api.pathname)
      || !/^[a-z0-9]+(?:-[a-z0-9]+)*\.auth\.[a-z]{2}-[a-z]+-\d\.amazoncognito\.com$/.test(cognito.hostname)
      || cognito.pathname !== "/" || typeof raw.clientId !== "string" || !/^[a-z0-9]{1,128}$/.test(raw.clientId)
      || typeof raw.extensionId !== "string" || !/^[a-p]{32}$/.test(raw.extensionId)
      || !/^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\.s3(?:\.[a-z]{2}-[a-z]+-\d)?\.amazonaws\.com$/.test(result.hostname)
      || !/^\/serving\/(?:development|production)(?:\/[A-Za-z0-9_-]+)*\/?$/.test(result.pathname)) invalid();
  const schedule = raw.pollScheduleSeconds ?? [2, 5, 10];
  if (!Array.isArray(schedule) || schedule.length !== 3 || schedule.some((value, index) => value !== [2, 5, 10][index])) invalid();
  const maximum = raw.maxComments ?? 1000;
  if (!Number.isSafeInteger(maximum) || maximum < 1 || maximum > 1000) invalid();
  return Object.freeze({enabled: true, apiBaseUrl: api.href.replace(/\/$/, ""),
    cognitoDomain: cognito.origin, clientId: raw.clientId, extensionId: raw.extensionId,
    resultBaseUrl: result.href.replace(/\/$/, ""), pollScheduleSeconds: Object.freeze([...schedule]), maxComments: maximum});
}
