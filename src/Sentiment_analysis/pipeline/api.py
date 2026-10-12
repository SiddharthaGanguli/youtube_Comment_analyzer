"""HTTP use cases: admission/status/download/deletion, with no fetch or prediction."""

from dataclasses import dataclass

from src.Sentiment_analysis.entity.analysis import Analysis, TERMINAL_STATES
from src.Sentiment_analysis.repositories.jobs import JobError


@dataclass(frozen=True)
class AnalysisReply:
    analysis: Analysis
    status_code: int


class APIPipeline:
    def __init__(self, settings, jobs, objects, models, *, fixture_mode=False):
        if fixture_mode and settings.environment != "development":
            raise ValueError("Fixture admission is allowed only in development")
        self.settings = settings
        self.jobs = jobs
        self.objects = objects
        self.models = models
        self.fixture_mode = fixture_mode

    def _current(self):
        manifest = self.models.current()
        model = manifest.details.model
        expected = self.settings.model
        if (expected.release_id is None or expected.code_revision is None or
                model.release_id != expected.release_id or model.sha256 != expected.sha256 or
                model.preprocessing_sha256 != expected.preprocessing_sha256 or
                model.result_schema_version != expected.result_schema_version or
                model.review_confidence_threshold != self.settings.worker.review_confidence_threshold or
                manifest.code_revision != expected.code_revision):
            raise JobError("DEPENDENCY_UNAVAILABLE")
        return manifest.details

    def create(self, owner, key, input):
        if not self.fixture_mode and not self.settings.youtube.live_enabled:
            raise JobError("LIVE_ANALYSIS_DISABLED")
        if input.max_comments > self.settings.youtube.maximum_comments:
            raise JobError("INVALID_INPUT")
        admission = self.jobs.admit(owner, key, input, self._current().model)
        analysis = self.jobs.status(owner, admission.request.analysis_id)
        return AnalysisReply(analysis, 200 if analysis.state in TERMINAL_STATES else 202)

    def _owned_current_status(self, owner, analysis_id):
        _, work = self.jobs.owned(owner, analysis_id)
        if work.state not in TERMINAL_STATES and work.deadline_at <= self.jobs.clock():
            self.jobs.expire(work.work_id)
        return self.jobs.status(owner, analysis_id)

    def status(self, owner, analysis_id):
        self.jobs.charge_read(owner)
        return self._owned_current_status(owner, analysis_id)

    def download(self, owner, analysis_id):
        self.jobs.charge_read(owner)
        status = self._owned_current_status(owner, analysis_id)
        if status.state == "FAILED":
            raise JobError("ANALYSIS_FAILED")
        return self.objects.download(self.jobs, owner, analysis_id)

    def delete(self, owner, analysis_id):
        self.jobs.charge_read(owner)
        self.jobs.delete_owned(owner, analysis_id)

    def current_model(self, owner):
        self.jobs.charge_read(owner)
        return self._current()
