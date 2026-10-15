import {validateClientConfig} from "./client-config.mjs";

function invalid() { throw new Error("The website connection settings are invalid"); }

// Website callbacks are a separate client type. The extension's Chromium
// callback validation stays strict and never accepts a website callback.
export function validateWebClientConfig(raw = {enabled: false}) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw) || typeof raw.enabled !== "boolean") invalid();
  const {redirectUri, ...shared} = raw;
  if (!raw.enabled) {
    if (Object.keys(raw).length !== 1) invalid();
    return Object.freeze({enabled: false});
  }
  if (Object.hasOwn(raw, "extensionId") || typeof redirectUri !== "string" || redirectUri.length > 2048
      || /[\s\x00-\x1f\x7f\\%]/.test(redirectUri) || /(?:^|\/)\.{1,2}(?:\/|$)/.test(redirectUri)) invalid();
  let redirect;
  try { redirect = new URL(redirectUri); } catch { invalid(); }
  if (redirect.username || redirect.password || redirect.search || redirect.hash
      || !/^\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.html$/.test(redirect.pathname)
      || redirect.href !== redirectUri) invalid();
  const development = redirect.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(redirect.hostname);
  if (!development && (redirect.protocol !== "https:" || redirect.port
      || !/^[a-z0-9.-]+$/.test(redirect.hostname) || redirect.hostname === "localhost"
      || /^\d+(?:\.\d+){3}$/.test(redirect.hostname))) invalid();
  // Reuse all backend, result-store, scope-client and polling constraints. The
  // placeholder is internal to validation and is never a registered app ID.
  const {extensionId: _ignored, ...checked} = validateClientConfig({...shared, extensionId: "a".repeat(32)});
  return Object.freeze({...checked, redirectUri});
}
