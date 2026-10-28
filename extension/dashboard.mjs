import {configureService, showUnavailable} from "./dashboard-app.mjs";

// Website deployment metadata is separate from the extension's Chromium client.
// A disabled public build imports no authentication or API modules.
export async function connectWebService(settings, {runtime = globalThis, dependencies} = {}) {
  try {
    if (settings && settings.enabled === false && Object.keys(settings).length === 1) {
      showUnavailable();
      return null;
    }
    const {validateWebClientConfig} = await import("./web-client-config.mjs");
    const config = validateWebClientConfig(settings);
    if (!config.enabled) {showUnavailable(); return null;}
    const current = new URL(runtime.location.href), redirect = new URL(config.redirectUri);
    if (current.origin !== redirect.origin || current.pathname !== redirect.pathname
        || !current.pathname.endsWith("/dashboard.html")) throw new Error("Website callback does not match this dashboard");
    // A direct demo link opts into the existing handwritten sample. OAuth
    // callbacks always keep their real workspace, including rejected callbacks.
    const callbackKeys = ["code", "state", "error", "error_description", "access_token", "id_token",
      "refresh_token", "token_type", "expires_in", "scope"];
    const fragment = new URLSearchParams(current.hash.slice(1));
    const hasCallback = callbackKeys.some(key => current.searchParams.has(key) || fragment.has(key));
    const startInDemo = current.searchParams.getAll("demo").length === 1
      && current.searchParams.get("demo") === "1" && !hasCallback;
    const [{WebAuthSession}, {AnalysisClient}, {JobController}, {resultToView}] = dependencies
      ? [{WebAuthSession: dependencies.WebAuthSession}, {AnalysisClient: dependencies.AnalysisClient},
        {JobController: dependencies.JobController}, {resultToView: dependencies.resultToView}]
      : await Promise.all([import("./web-auth-session.mjs"), import("./analysis-client.mjs"),
        import("./job-controller.mjs"), import("./result-model.mjs")]);
    const auth = new WebAuthSession({config, locationApi: runtime.location, historyApi: runtime.history,
      storage: runtime.sessionStorage});
    const api = new AnalysisClient({config, auth, configValidator: validateWebClientConfig});
    const controller = new JobController({api, auth, pollScheduleSeconds: config.pollScheduleSeconds, maxComments: config.maxComments});
    configureService(controller, resultToView, {startInService: !startInDemo});
    await controller.initialize();
    // A full-page sign-in leaves a pending in-memory promise. Always discard
    // that session on navigation; only its short-lived PKCE record survives.
    runtime.addEventListener("pagehide", () => controller.dispose());
    runtime.addEventListener("pageshow", event => {if (event.persisted) runtime.location.reload();});
    return controller;
  } catch {
    showUnavailable();
    return null;
  }
}

if (location.protocol === "chrome-extension:") {
  try {
    const {clientSettings} = await import("./client-settings.mjs");
    const {validateClientConfig} = await import("./client-config.mjs");
    const config = validateClientConfig(clientSettings);
    if (config.enabled) {
      const [{AuthSession}, {AnalysisClient}, {JobController}, {resultToView}] = await Promise.all([
        import("./auth-session.mjs"), import("./analysis-client.mjs"), import("./job-controller.mjs"), import("./result-model.mjs"),
      ]);
      const auth = new AuthSession({config});
      const api = new AnalysisClient({config, auth});
      const controller = new JobController({api, auth, pollScheduleSeconds: config.pollScheduleSeconds, maxComments: config.maxComments});
      configureService(controller, resultToView);
      await controller.initialize();
      chrome.storage.onChanged.addListener((_changes, area) => {if (area === "session") controller.syncSession();});
      window.addEventListener("pagehide", event => {if (event.persisted) controller.pause(); else controller.dispose();});
    }
  } catch {showUnavailable();}
} else if (location.protocol === "https:" || location.protocol === "http:"
  && ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) {
  try {
    const {webClientSettings} = await import("./web-client-settings.mjs");
    await connectWebService(webClientSettings);
  } catch {showUnavailable();}
}
