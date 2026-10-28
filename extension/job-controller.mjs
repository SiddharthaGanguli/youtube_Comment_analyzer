// Own one visible analysis. Filters never call this controller or the network.
import { youtubeVideoUrl } from "./lib.mjs";

const TERMINAL = new Set(["SUCCEEDED", "PARTIAL", "FAILED"]);
export class JobController {
  #sessionToken = null;
  constructor({api, auth, onChange = () => {}, pollScheduleSeconds = [2, 5, 10], maxComments = 1000,
    now = Date.now, random = Math.random,
    // Window timers require their browser receiver, even when called through this controller.
    setTimer = (callback, delay) => globalThis.setTimeout(callback, delay),
    clearTimer = handle => globalThis.clearTimeout(handle),
    isHidden = () => globalThis.document?.hidden ?? false, uuid = () => crypto.randomUUID()}) {
    Object.assign(this, {api, auth, onChange, pollScheduleSeconds, maxComments, now, random, setTimer, clearTimer, isHidden, uuid});
    this.state = {phase: "signed-out", signedIn: false, busy: false, analysis: null, result: null, error: null, modelDetails: null};
    this.revision = 0;
    this.pollIndex = 0;
    this.lastSubmission = null;
    this.disposed = false;
    this.suspended = false;
    this.pollFailures = 0;
    this.retryAt = 0;
  }
  emit(update) {
    if (!this.disposed) {
      this.state = {...this.state, ...update};
      // A rendering failure must not prevent cancellation or secure sign-out.
      try { this.onChange(this.state); } catch { /* The UI owns its rendering errors. */ }
    }
  }
  cancel() {
    this.revision++;
    this.abort?.abort();
    this.clearTimer(this.pollTimer);
    this.clearTimer(this.expiryTimer);
    this.abort = new AbortController();
    return this.revision;
  }
  current(revision) { return !this.disposed && revision === this.revision; }
  async initialize() {
    const revision = this.cancel();
    this.lastSubmission = null;
    this.#sessionToken = null;
    this.emit({phase: "initializing", analysis: null, result: null, modelDetails: null, error: null, busy: true});
    try {
      await this.auth.initialize();
      const token = this.auth.signedIn ? await this.auth.getAccessToken() : null;
      if (this.current(revision)) {
        this.#sessionToken = token;
        this.emit({phase: token ? "ready" : "signed-out", signedIn: !!token, busy: false});
      }
    } catch (error) { this.fail(error, revision); }
  }
  get canSignUp() { return typeof this.auth.signUp === "function"; }
  signIn() { return this.#authenticate("signIn"); }
  signUp() { return this.#authenticate("signUp"); }
  async #authenticate(method) {
    if (this.state.busy) return;
    if (typeof this.auth[method] !== "function") {
      this.emit({error: {code: "AUTH_UNAVAILABLE", retryable: false}});
      return;
    }
    const revision = this.cancel();
    this.lastSubmission = null;
    this.#sessionToken = null;
    this.emit({phase: method === "signUp" ? "signing-up" : "signing-in", busy: true, error: null, analysis: null, result: null, modelDetails: null});
    try {
      await this.auth[method]();
      if (!this.current(revision)) return;
      const token = await this.auth.getAccessToken();
      if (this.current(revision)) {
        this.#sessionToken = token;
        this.retryAt = 0;
        this.emit({phase: "ready", signedIn: true, busy: false});
      }
    } catch (error) { this.fail(error, revision); }
  }
  async signOut() {
    const revision = this.cancel();
    this.lastSubmission = null;
    this.#sessionToken = null;
    this.retryAt = 0;
    this.emit({phase: "signed-out", signedIn: false, busy: false, analysis: null, result: null, modelDetails: null, error: null});
    try { await this.auth.signOut(); } catch (error) { this.fail(error, revision); }
  }
  // Called when trusted session storage changes in another extension window.
  async syncSession() {
    // Own token writes happen before AuthSession.signIn finishes its fenced
    // read-back. AuthSession itself aborts a true remote sign-out during login.
    if (this.disposed || ["signing-in", "signing-up"].includes(this.state.phase)) return;
    const revision = this.revision;
    try {
      const token = await this.auth.getAccessToken();
      if (!this.current(revision)) return;
      if (token !== this.#sessionToken) {
        this.cancel();
        this.#sessionToken = token;
        this.lastSubmission = null;
        this.retryAt = 0;
        this.emit({phase: "ready", signedIn: true, busy: false, analysis: null, result: null, modelDetails: null, error: null});
      }
    } catch {
      if (!this.current(revision)) return;
      this.cancel();
      this.#sessionToken = null;
      this.lastSubmission = null;
      this.emit({phase: "signed-out", signedIn: false, busy: false, analysis: null, result: null, modelDetails: null, error: null});
    }
  }
  async submit(value) {
    if (this.disposed || this.state.busy || this.now() < this.retryAt) return;
    const videoUrl = youtubeVideoUrl(value);
    if (!videoUrl) { this.emit({error: {code: "INVALID_INPUT"}}); return; }
    if (!this.state.signedIn) { this.emit({phase: "signed-out", error: {code: "UNAUTHORIZED"}}); return; }
    this.lastSubmission = {videoUrl, key: this.uuid()};
    await this.sendSubmission();
  }
  async sendSubmission() {
    if (!this.lastSubmission || this.disposed) return;
    const revision = this.cancel();
    this.pollIndex = 0;
    this.pollFailures = 0;
    this.emit({phase: "submitting", busy: true, analysis: null, result: null, modelDetails: null, error: null});
    try {
      const analysis = await this.api.submit(this.lastSubmission.videoUrl, this.lastSubmission.key, {signal: this.abort.signal});
      if (this.current(revision)) await this.accept(analysis, revision);
    } catch (error) { this.fail(error, revision); }
  }
  async accept(analysis, revision) {
    if (!this.current(revision)) return;
    this.emit({analysis, phase: analysis.state, busy: !TERMINAL.has(analysis.state), error: null});
    if (this.expired()) return;
    this.armExpiry();
    this.retryAt = 0;
    if (analysis.state === "FAILED") { this.emit({phase: "failed", busy: false, error: analysis.failure}); return; }
    if (!TERMINAL.has(analysis.state)) { this.schedulePoll(); return; }
    this.clearTimer(this.pollTimer);
    if (this.state.result) {
      this.emit({phase: this.resultPhase(this.state.result), busy: false});
      return;
    }
    if (this.loadingRevision === revision) return;
    this.loadingRevision = revision;
    this.emit({phase: "loading-result", busy: true});
    try {
      const result = await this.api.download(analysis.analysis_id, {signal: this.abort.signal, analysis});
      if (!this.current(revision) || this.expired()) return;
      const phase = this.resultPhase(result);
      this.emit({phase, busy: false, result});
      // Current-release metrics must never be attributed to an older result.
      try {
        const details = await this.api.currentModel({signal: this.abort.signal});
        if (this.current(revision) && this.state.result && details.model.release_id === result.model.release_id)
          this.emit({modelDetails: details});
      } catch { /* A result is useful even when optional model details are unavailable. */ }
    } catch (error) { this.fail(error, revision); }
    finally { if (this.loadingRevision === revision) this.loadingRevision = null; }
  }
  resultPhase(result) {
    return !result.summary.received ? "empty" : !result.summary.analyzed ? "all-skipped" : result.state === "PARTIAL" ? "partial" : "complete";
  }
  expired() {
    const value = this.state.analysis?.expires_at;
    if (value && this.now() >= Date.parse(value)) {
      this.cancel();
      this.emit({phase: "expired", busy: false, result: null, modelDetails: null, error: null});
      return true;
    }
    return false;
  }
  armExpiry() {
    this.clearTimer(this.expiryTimer);
    const expires = Date.parse(this.state.analysis.expires_at);
    this.expiryTimer = this.setTimer(() => this.expired(), Math.max(0, expires - this.now()));
  }
  schedulePoll(minimumSeconds = 0) {
    this.clearTimer(this.pollTimer);
    if (this.disposed || this.suspended || this.isHidden() || this.expired() || this.pollFailures >= 5) return;
    const index = Math.min(this.pollIndex++, this.pollScheduleSeconds.length - 1);
    const jittered = this.pollScheduleSeconds[index] * (0.8 + this.random() * 0.4);
    const seconds = Math.max(2, minimumSeconds, jittered, (this.retryAt - this.now()) / 1000);
    this.nextPollAt = this.now() + seconds * 1000;
    const revision = this.revision;
    this.pollTimer = this.setTimer(() => { if (this.current(revision)) return this.refresh(); }, Math.min(2_147_483_647, seconds * 1000));
  }
  async refresh() {
    if (this.disposed || this.suspended || !this.state.analysis || this.isHidden() || this.expired()
        || this.refreshRevision === this.revision) return;
    if (this.now() < this.retryAt) { this.schedulePoll(); return; }
    const revision = this.revision;
    this.refreshRevision = revision;
    this.clearTimer(this.pollTimer);
    try {
      const analysis = await this.api.status(this.state.analysis.analysis_id, {signal: this.abort.signal});
      if (this.current(revision)) { this.pollFailures = 0; await this.accept(analysis, revision); }
    } catch (error) {
      this.fail(error, revision);
      if (this.current(revision) && error.retryable && this.state.signedIn && ++this.pollFailures < 5)
        this.schedulePoll(Math.max(10, error.retryAfterSeconds || 0));
    } finally { if (this.refreshRevision === revision) this.refreshRevision = null; }
  }
  visibilityChanged() {
    if (this.expired()) return;
    this.clearTimer(this.pollTimer);
    if (!this.suspended && !this.isHidden() && this.state.analysis && !TERMINAL.has(this.state.analysis.state)) {
      const remaining = Math.max(0, (this.nextPollAt - this.now()) / 1000) || 0;
      this.schedulePoll(remaining);
    }
  }
  async retry() {
    if (this.disposed || this.suspended || this.isHidden() || this.state.busy || this.now() < this.retryAt
        || this.state.error && !this.state.error.retryable) return;
    if (this.state.phase === "failed") return;
    this.pollFailures = 0;
    if (this.state.analysis && !this.expired()) {
      this.emit({error: null, busy: true});
      await this.refresh();
    } else if (this.lastSubmission && !this.state.analysis) await this.sendSubmission();
  }
  pause() {
    this.suspended = true;
    this.cancel();
    if (this.state.analysis && !this.expired()) this.armExpiry();
    if (this.state.busy) this.emit({phase: "paused", busy: false});
  }
  async resume() {
    this.suspended = false;
    if (this.expired()) return;
    if (this.pollFailures >= 5 && this.state.error) return;
    if (this.state.analysis && !this.state.result) {
      if (this.now() < this.retryAt) this.schedulePoll();
      else await this.accept(this.state.analysis, this.revision);
    } else if (!this.state.analysis && this.state.phase === "paused") {
      if (this.lastSubmission) await this.sendSubmission();
      else this.emit({phase: this.state.signedIn ? "ready" : "signed-out"});
    }
  }
  async remove() {
    if (!this.state.analysis || this.disposed) return;
    const analysis = this.state.analysis;
    const revision = this.cancel();
    this.armExpiry();
    this.emit({busy: true, error: null});
    try {
      await this.api.delete(analysis.analysis_id, {signal: this.abort.signal});
      if (this.current(revision)) {
        this.lastSubmission = null;
        this.emit({phase: "deleted", busy: false, analysis: null, result: null, modelDetails: null});
      }
    } catch (error) { this.fail(error, revision); }
  }
  fail(error, revision) {
    if (!this.current(revision)) return;
    const code = error?.code || "NETWORK_ERROR";
    if (code === "UNAUTHORIZED" || code === "AUTH_REQUIRED") {
      this.cancel();
      this.#sessionToken = null;
      this.lastSubmission = null;
      this.emit({phase: "signed-out", signedIn: false, busy: false, analysis: null, result: null, modelDetails: null, error: {code: "UNAUTHORIZED"}});
    } else if (code === "EXPIRED") {
      this.cancel();
      this.emit({phase: "expired", busy: false, result: null, modelDetails: null, error: null});
    } else {
      if (this.state.analysis && this.expired()) return;
      if (this.state.analysis) this.armExpiry();
      const safeError = {code, retryable: Boolean(error?.retryable), retryAfterSeconds: error?.retryAfterSeconds || null, resetAt: error?.resetAt || null};
      const resetAt = Date.parse(safeError.resetAt);
      this.retryAt = Math.max(this.now() + (safeError.retryAfterSeconds || 0) * 1000, Number.isFinite(resetAt) ? resetAt : 0);
      this.emit({phase: "error", busy: false, error: safeError});
    }
  }
  dispose() {
    this.cancel(); this.lastSubmission = null; this.#sessionToken = null;
    this.emit({phase: "signed-out", signedIn: false, busy: false, analysis: null, result: null, modelDetails: null, error: null});
    this.disposed = true; this.auth.dispose?.();
  }
}
