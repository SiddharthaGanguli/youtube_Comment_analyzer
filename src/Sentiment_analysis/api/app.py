"""Application factory. The default app serves liveness/docs without AWS calls."""

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.exceptions import HTTPException

from src.Sentiment_analysis.api.errors import error_response
from src.Sentiment_analysis.api.middleware import RequestBoundary
from src.Sentiment_analysis.api.routes import router
from src.Sentiment_analysis.config.serving import ServingConfigurationManager
from src.Sentiment_analysis.entity.analysis import AnalysisResult
from src.Sentiment_analysis.repositories.jobs import JobError


def create_app(settings=None, *, pipeline=None, pipeline_factory=None, clock=None):
    if settings is None:
        import os
        settings = ServingConfigurationManager(os.environ.get("SERVING_CONFIG_PATH")).get_serving_config()
    if pipeline_factory is None:
        from src.Sentiment_analysis.config.serving_runtime import api_runtime
        pipeline_factory = api_runtime
    clock = clock or (lambda: datetime.now(timezone.utc))
    development = settings.environment == "development"
    app = FastAPI(title="Comment Lens serving API", version="0.1.0", redirect_slashes=False,
                  description="Owned asynchronous analyses of latest public top-level YouTube comments.",
                  docs_url="/docs" if development else None, redoc_url=None,
                  openapi_url="/openapi.json" if development else None, separate_input_output_schemas=False)
    app.state.settings = settings
    app.state.pipeline = pipeline
    app.state.pipeline_factory = pipeline_factory
    app.state.clock = clock
    app.include_router(router)

    @app.exception_handler(JobError)
    async def handle_domain(request: Request, error: JobError):
        return error_response(error.code, request.scope, clock())

    @app.exception_handler(RequestValidationError)
    async def handle_input(request: Request, error: RequestValidationError):
        return error_response("INVALID_INPUT", request.scope, clock())

    @app.exception_handler(HTTPException)
    async def handle_http(request: Request, error: HTTPException):
        code = "NOT_FOUND" if error.status_code == 404 else "INVALID_INPUT"
        response = error_response(code, request.scope, clock())
        response.status_code = error.status_code
        if error.status_code == 405 and error.headers and "Allow" in error.headers:
            response.headers["Allow"] = error.headers["Allow"]
        return response

    # CORS wraps the boundary so safe dependency errors are readable by the
    # allowed extension origin too. Preflight does not need authentication.
    app.add_middleware(RequestBoundary, maximum_bytes=settings.api.maximum_request_body_bytes, clock=clock)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.api.allowed_origins),
                       allow_credentials=False, allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
                       expose_headers=["Location", "Retry-After", "X-Request-ID"], max_age=600)

    def schema():
        if app.openapi_schema is None:
            document = get_openapi(title=app.title, version=app.version, description=app.description,
                                   routes=app.routes, separate_input_output_schemas=False)
            result = AnalysisResult.model_json_schema(mode="serialization", ref_template="#/components/schemas/{model}")
            definitions = result.pop("$defs", {})
            for name, definition in definitions.items():
                document["components"]["schemas"].setdefault(name, definition)
            document["components"]["schemas"]["AnalysisResult"] = result
            for path in document["paths"].values():
                for operation in path.values():
                    if isinstance(operation, dict) and "security" in operation:
                        # HTTP bearer schemes have no OAuth flow in OpenAPI; the
                        # real scope requirements are enforced by the dependency.
                        operation["security"] = [{"CognitoAccessToken": []}]
            document["paths"]["/health/live"]["get"]["security"] = []
            app.openapi_schema = document
        return app.openapi_schema

    app.openapi = schema
    return app


app = create_app()
