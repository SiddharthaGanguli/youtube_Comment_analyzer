// The homepage only reflects the current tab's account state. API permission
// still comes from the backend's token validation, never this header.
export async function configureHomeSession(settings, {runtime = globalThis, documentApi = runtime.document,
  now = Date.now, dependencies} = {}) {
  const guestLinks = [...documentApi.querySelectorAll("[data-home-guest]")];
  const status = documentApi.querySelector("#home-account-status");
  const signOut = documentApi.querySelector("#home-sign-out");
  const message = documentApi.querySelector("#home-session-message");
  const display = signedIn => {
    for (const link of guestLinks) link.hidden = signedIn;
    if (status) status.hidden = !signedIn;
    if (signOut) signOut.hidden = !signedIn;
  };
  display(false);
  if (!settings?.enabled || !signOut) return null;
  try {
    const [{validateWebClientConfig}, sessionStore] = dependencies
      ? [{validateWebClientConfig: dependencies.validateWebClientConfig}, dependencies]
      : await Promise.all([import("./web-client-config.mjs"), import("./web-session-store.mjs")]);
    const config = validateWebClientConfig(settings);
    if (!config.enabled) return null;
    const current = new URL(runtime.location.href), callback = new URL(config.redirectUri);
    const homepage = new URL("index.html", callback), directory = new URL(".", callback);
    if (current.origin !== callback.origin || current.username || current.password
        || ![homepage.pathname, directory.pathname].includes(current.pathname)) return null;
    const storage = runtime.sessionStorage;
    const {readWebSession, clearWebSession, WEB_SESSION_SKEW_MS} = sessionStore;
    const setTimer = runtime.setTimeout.bind(runtime), clearTimer = runtime.clearTimeout.bind(runtime);
    let timer = null, disposed = false;
    function stopTimer() {if (timer !== null) clearTimer(timer); timer = null;}
    function refresh() {
      if (disposed) return;
      stopTimer();
      try {
        const session = readWebSession(config, {storage, now});
        display(Boolean(session));
        if (session) timer = setTimer(refresh, Math.max(1, session.expiresAtMs - now() - WEB_SESSION_SKEW_MS + 1));
      } catch {display(false);}
    }
    function endSession() {
      if (disposed) return;
      try {
        clearWebSession(config, {storage});
        storage.removeItem(`commentLens.webPkce.${config.clientId}.${config.redirectUri}`);
        if (message) {message.hidden = true; message.textContent = "";}
        refresh();
      } catch {
        if (message) {
          message.textContent = "Sign out could not be completed. Please try again.";
          message.hidden = false;
        }
      }
    }
    signOut.addEventListener("click", endSession);
    runtime.addEventListener("pageshow", refresh);
    runtime.addEventListener("focus", refresh);
    runtime.addEventListener("pagehide", stopTimer);
    refresh();
    return {refresh, dispose() {
      disposed = true; stopTimer();
      signOut.removeEventListener("click", endSession);
      runtime.removeEventListener("pageshow", refresh);
      runtime.removeEventListener("focus", refresh);
      runtime.removeEventListener("pagehide", stopTimer);
    }};
  } catch {display(false); return null;}
}

if (typeof location !== "undefined" && (location.protocol === "https:" || location.protocol === "http:"
  && ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname))) {
  try {
    const {webClientSettings} = await import("./web-client-settings.mjs");
    await configureHomeSession(webClientSettings);
  } catch { /* Static Login and Sign up links remain available. */ }
}
