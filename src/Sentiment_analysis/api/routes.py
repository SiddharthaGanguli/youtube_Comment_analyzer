"""Keep routes small: authentication and types here, use cases in APIPipeline."""

from uuid import UUID

from fastapi import APIRouter, Header, Request, Response, Security

from src.Sentiment_analysis.api.auth import authenticated_principal
from src.Sentiment_analysis.api.errors import ERRORS
from src.Sentiment_analysis.entity.analysis import Analysis, AnalysisInput, ErrorEnvelope, ModelDetails, ResultDownload
from src.Sentiment_analysis.entity.api import Liveness, Principal

router = APIRouter()


def failures(*codes):
    return {status: {"model": ErrorEnvelope, "description": "; ".join(
        message for code, (code_status, message, _) in ERRORS.items() if code_status == status and code in codes)}
        for status in {ERRORS[code][0] for code in codes}}


AUTH = ("UNAUTHORIZED", "FORBIDDEN", "RATE_LIMITED", "DEPENDENCY_UNAVAILABLE")
OWNED = (*AUTH, "NOT_FOUND", "EXPIRED", "INVALID_INPUT")


def pipeline(request):
    if request.app.state.pipeline is None:
        request.app.state.pipeline = request.app.state.pipeline_factory()
    return request.app.state.pipeline


@router.post("/v1/analyses", response_model=Analysis, status_code=202, tags=["Analyses"],
             operation_id="createAnalysis", summary="Create an owned analysis or join identical work",
             description="Requires analysis/write. Body limit 2048 bytes. Use one UUID retry key per canonical submission.",
             responses={200: {"model": Analysis, "description": "Completed cache hit or terminal idempotent replay"},
                        202: {"description": "New or shared nonterminal work", "headers": {
                            "Location": {"schema": {"type": "string"}},
                            "Retry-After": {"schema": {"type": "integer", "minimum": 1}}}},
                        **failures(*AUTH, "EXPIRED", "INVALID_INPUT", "REQUEST_TOO_LARGE", "IDEMPOTENCY_CONFLICT",
                                   "ACTIVE_JOB_LIMIT", "SYSTEM_BUSY", "YOUTUBE_QUOTA_EXHAUSTED", "LIVE_ANALYSIS_DISABLED")})
def create_analysis(input: AnalysisInput, request: Request, response: Response,
                    idempotency_key: UUID = Header(alias="Idempotency-Key"),
                    principal: Principal = Security(authenticated_principal, scopes=["analysis/write"])):
    reply = pipeline(request).create(principal.owner, idempotency_key, input)
    response.status_code = reply.status_code
    if reply.status_code == 202:
        response.headers["Location"] = f"/v1/analyses/{reply.analysis.analysis_id}"
        response.headers["Retry-After"] = str(request.app.state.settings.client.poll_schedule_seconds[0])
    return reply.analysis


@router.get("/v1/analyses/{analysis_id}", response_model=Analysis, tags=["Analyses"],
            operation_id="getAnalysis", summary="Read an owned analysis status",
            description="Requires analysis/read. Failed jobs return 200 with state FAILED.", responses=failures(*OWNED))
def get_analysis(analysis_id: UUID, request: Request,
                 principal: Principal = Security(authenticated_principal, scopes=["analysis/read"])):
    return pipeline(request).status(principal.owner, analysis_id)


@router.get("/v1/analyses/{analysis_id}/result", response_model=ResultDownload, tags=["Analyses"],
            operation_id="getResultDownload", summary="Authorize a private S3 result download",
            description="Requires analysis/read. Returns a short-lived descriptor; downloaded JSON follows AnalysisResult.",
            responses={200: {"headers": {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}}},
                       **failures(*OWNED, "RESULT_NOT_READY", "ANALYSIS_FAILED")})
def get_download(analysis_id: UUID, request: Request,
                 principal: Principal = Security(authenticated_principal, scopes=["analysis/read"])):
    return pipeline(request).download(principal.owner, analysis_id)


@router.delete("/v1/analyses/{analysis_id}", status_code=204, response_class=Response, tags=["Analyses"],
               operation_id="deleteAnalysis", summary="Remove the owner's reference",
               description="Requires analysis/write. Repeated deletion of an owned tombstone returns 204; shared work continues.",
               responses=failures(*OWNED))
def delete_analysis(analysis_id: UUID, request: Request,
                    principal: Principal = Security(authenticated_principal, scopes=["analysis/write"])):
    pipeline(request).delete(principal.owner, analysis_id)
    return Response(status_code=204)


@router.get("/v1/models/current", response_model=ModelDetails, tags=["Models"],
            operation_id="getCurrentModel", summary="Explain the currently selected serving release",
            description="Requires analysis/read. Evaluation measures supplied-label agreement; no artifact paths are exposed.",
            responses=failures(*AUTH))
def current_model(request: Request,
                  principal: Principal = Security(authenticated_principal, scopes=["analysis/read"])):
    return pipeline(request).current_model(principal.owner)


@router.get("/health/live", response_model=Liveness, tags=["Health"],
            operation_id="getLiveness", summary="Check that the API process can respond",
            description="Public process check; not a dependency or worker readiness check.")
def liveness():
    return Liveness()
