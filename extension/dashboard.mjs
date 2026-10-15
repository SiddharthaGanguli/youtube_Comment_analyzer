import {configureService, showUnavailable} from "./dashboard-app.mjs";

// The public website always stays in demo mode. Account access is extension-only.
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
}
