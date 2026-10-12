"""HTTP contract tests using verified-context fixtures and mocked AWS adapters."""

import asyncio
import json
import subprocess
import sys
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from mangum import Mangum

from src.Sentiment_analysis.api.app import create_app
from src.Sentiment_analysis.api.middleware import RequestBoundary
from src.Sentiment_analysis.entity.analysis import CONFIDENCE_DEFINITION, ModelDetails
from src.Sentiment_analysis.entity.api import ReleaseManifest, release_fingerprint
from src.Sentiment_analysis.pipeline.api import APIPipeline
from src.Sentiment_analysis.repositories.dynamo import Change
from src.Sentiment_analysis.repositories.models import ModelRepository
from tests.serving.aws_fixtures import ROOT, StorageFixture

ALICE = "00000000-0000-4000-8000-000000000001"
BOB = "00000000-0000-4000-8000-000000000002"
ORIGIN = "chrome-extension://" + "a" * 32
CODE = "4" * 40


class GatewayContext:
    """Test-only gateway stand-in; never shipped as an authentication mode."""

    def __init__(self, app, fixture):
        self.app = app
        self.fixture = fixture

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        token = headers.get(b"authorization", b"").decode()
        claims = self.fixture.claims_for(token)
        if claims is not None:
            scope["aws.event"] = {"version": "2.0", "requestContext": {"authorizer": {"jwt": {
                "claims": claims, "scopes": claims["scope"].split()}}}}
        await self.app(scope, receive, send)


class APITests(StorageFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.model = self.model.model_copy(update={"release_id": release_fingerprint(self.model, CODE)})
        self.config = self.config.model_copy(update={
            "model": self.config.model.model_copy(update={"release_id": self.model.release_id, "code_revision": CODE}),
            "auth": self.config.auth.model_copy(update={"cognito_issuer": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Fixture",
                                                        "public_client_id": "fixtureclient1"}),
            "api": self.config.api.model_copy(update={"allowed_origins": (ORIGIN,)}),
        })
        self.jobs.settings = self.config
        details = ModelDetails(model=self.model, type="TF-IDF + LogisticRegression",
                               labels={"0": "Negative", "1": "Neutral", "2": "Positive"},
                               confidence_definition=CONFIDENCE_DEFINITION,
                               evaluation={"test_accuracy": 0.71, "test_macro_f1": 0.70, "test_rows": 100,
                                           "labels_human_confirmed": False, "context": "Synthetic fixture supplied-label agreement"})
        self.manifest = ReleaseManifest(code_revision=CODE, details=details)
        self.models = ModelRepository(self.store)
        self.models.register(self.manifest)
        self.models.promote(self.model.release_id, expected_release_id=None)
        self.pipeline = APIPipeline(self.config, self.jobs, self.objects, self.models, fixture_mode=True)
        self.app = create_app(self.config, pipeline=self.pipeline, clock=lambda: self.now)
        self.app.add_middleware(GatewayContext, fixture=self)
        self.client = TestClient(self.app, headers={"Authorization": "Bearer fixture-alice"})
        self.addCleanup(self.client.close)
        self.contract = yaml.safe_load((ROOT / "docs/backend-contract.yaml").read_text(encoding="utf-8"))

    def claims_for(self, token):
        known = {"Bearer fixture-alice", "Bearer fixture-bob", "Bearer fixture-read", "Bearer fixture-write",
                 "Bearer fixture-expired", "Bearer fixture-id", "Bearer fixture-issuer", "Bearer fixture-client"}
        if token not in known:
            return None
        claims = {"sub": BOB if token == "Bearer fixture-bob" else ALICE,
                  "iss": self.config.auth.cognito_issuer, "client_id": self.config.auth.public_client_id,
                  "token_use": "access", "exp": str(int((self.now + timedelta(hours=1)).timestamp())),
                  "scope": "analysis/read analysis/write"}
        if token == "Bearer fixture-read":
            claims["scope"] = "analysis/read"
        elif token == "Bearer fixture-write":
            claims["scope"] = "analysis/write"
        elif token == "Bearer fixture-expired":
            claims["exp"] = str(int(self.now.timestamp()))
        elif token == "Bearer fixture-id":
            claims["token_use"] = "id"
        elif token == "Bearer fixture-issuer":
            claims["iss"] = "https://wrong-issuer.example"
        elif token == "Bearer fixture-client":
            claims["client_id"] = "wrongclient"
        return claims

    def submit(self, *, key=None, body=None, token="fixture-alice"):
        return self.client.post("/v1/analyses", json=body or self.input.model_dump(mode="json"),
                                headers={"Idempotency-Key": str(key or uuid4()), "Authorization": "Bearer " + token})

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-request-id"], response.json()["error"]["request_id"])
        self.assert_schema(response.json(), "ErrorEnvelope")

    def assert_schema(self, data, name):
        for document in (self.contract, self.app.openapi()):
            schema = {"$ref": f"#/components/schemas/{name}", "components": document["components"]}
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)

    def test_liveness_is_public_and_does_not_initialize_aws(self):
        factory = unittest.mock.Mock(side_effect=AssertionError("AWS must stay lazy"))
        plain = create_app(self.config, pipeline_factory=factory, clock=lambda: self.now)
        with TestClient(plain) as client:
            response = client.get("/health/live")
            self.assertEqual(response.json(), {"status": "ok"})
            self.assertEqual(response.status_code, 200)
            self.assert_error(client.get("/v1/models/current", headers={"Authorization": "Bearer unsigned"}), 401, "UNAUTHORIZED")
            factory.assert_not_called()

    def test_submission_returns_owned_location_and_safe_status_shape(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202, response.text)
        data = response.json()
        self.assert_schema(data, "Analysis")
        self.assertEqual(response.headers["location"], "/v1/analyses/" + data["analysis_id"])
        self.assertEqual(response.headers["retry-after"], "2")
        self.assertNotIn("work_id", data)
        self.assertNotIn("owner", data)
        status = self.client.get(response.headers["location"])
        self.assertEqual(status.status_code, 200)
        self.assert_schema(status.json(), "Analysis")
        self.assert_error(self.client.get(response.headers["location"] + "/result"), 409, "RESULT_NOT_READY")

    def test_canonical_idempotent_replay_and_changed_body_conflict(self):
        key = uuid4()
        first = self.submit(key=key)
        replay = self.submit(key=key, body={"video_url": self.input.video_url})
        self.assertEqual(replay.json()["analysis_id"], first.json()["analysis_id"])
        self.assertEqual(replay.status_code, 202)
        self.assert_error(self.submit(key=key, body={"video_url": self.input.video_url, "max_comments": 100}),
                          409, "IDEMPOTENCY_CONFLICT")
        request, work = self.jobs.owned(ALICE, first.json()["analysis_id"])
        self.complete(work)
        self.assertEqual(self.submit(key=key).status_code, 200)

    def test_fresh_completed_cache_has_new_owned_id_and_no_worker(self):
        first = self.submit()
        _, work = self.jobs.owned(ALICE, first.json()["analysis_id"])
        self.complete(work)
        bob = self.submit(token="fixture-bob")
        self.assertEqual(bob.status_code, 200)
        self.assertTrue(bob.json()["cache_hit"])
        self.assertNotEqual(bob.json()["analysis_id"], first.json()["analysis_id"])
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 0)
        self.assertEqual(self.store.get("QUOTA#2026-10-12").data["reserved"], 0)

    def test_foreign_and_unknown_ids_are_indistinguishable(self):
        first = self.submit()
        path = "/v1/analyses/" + first.json()["analysis_id"]
        for suffix in ("", "/result"):
            foreign = self.client.get(path + suffix, headers={"Authorization": "Bearer fixture-bob"})
            unknown = self.client.get("/v1/analyses/" + str(uuid4()) + suffix,
                                      headers={"Authorization": "Bearer fixture-bob"})
            self.assert_error(foreign, 404, "NOT_FOUND")
            self.assert_error(unknown, 404, "NOT_FOUND")
            self.assertEqual(foreign.json()["error"]["message"], unknown.json()["error"]["message"])
        self.assert_error(self.client.delete(path, headers={"Authorization": "Bearer fixture-bob"}), 404, "NOT_FOUND")

    def test_missing_and_invalid_contexts_cannot_authenticate(self):
        for token in ("unknown", "fixture-expired", "fixture-id", "fixture-issuer", "fixture-client"):
            self.assert_error(self.client.get("/v1/models/current", headers={"Authorization": "Bearer " + token}),
                              401, "UNAUTHORIZED")
        self.client.headers.pop("Authorization")
        self.assert_error(self.client.get("/v1/models/current", headers={"X-User-Id": ALICE}), 401, "UNAUTHORIZED")

    def test_read_and_write_scopes_are_required_without_charging_denied_calls(self):
        self.assert_error(self.submit(token="fixture-read"), 403, "FORBIDDEN")
        self.assert_error(self.client.get("/v1/models/current", headers={"Authorization": "Bearer fixture-write"}),
                          403, "FORBIDDEN")
        self.assertIsNone(self.store.get("LIMIT#GLOBAL#ACTIVE"))
        response = self.client.get("/v1/models/current", headers={"Authorization": "Bearer fixture-read"})
        self.assertEqual(response.status_code, 200)

    def test_invalid_body_header_uuid_and_owner_input_are_safe_errors(self):
        for body in ({"video_url": "http://localhost/secret"}, {"video_url": self.input.video_url, "max_comments": True},
                     {"video_url": self.input.video_url, "max_comments": 1001},
                     {"video_url": self.input.video_url, "owner": "secret-value-not-for-response"}):
            response = self.submit(body=body)
            self.assert_error(response, 422, "INVALID_INPUT")
            self.assertNotIn("secret-value-not-for-response", response.text)
        self.assert_error(self.client.post("/v1/analyses", json=self.input.model_dump(mode="json")), 422, "INVALID_INPUT")
        self.assert_error(self.client.post("/v1/analyses", json=self.input.model_dump(mode="json"),
                                          headers={"Idempotency-Key": "wrong"}), 422, "INVALID_INPUT")
        self.assert_error(self.client.get("/v1/analyses/wrong"), 422, "INVALID_INPUT")
        self.assertIsNone(self.store.get("LIMIT#GLOBAL#ACTIVE"))

    def test_exact_body_limit_counts_bytes_and_rejects_one_extra(self):
        body = json.dumps({"video_url": self.input.video_url}).encode().ljust(2048, b" ")
        headers = {"Content-Type": "application/json", "Idempotency-Key": str(uuid4())}
        self.assertEqual(self.client.post("/v1/analyses", content=body, headers=headers).status_code, 202)
        self.assert_error(self.client.post("/v1/analyses", content=body + b" ", headers=headers), 413, "REQUEST_TOO_LARGE")
        unicode_body = json.dumps({"video_url": self.input.video_url, "note": "é" * 1000}, ensure_ascii=False)
        self.assertLess(len(unicode_body), 2048)
        self.assert_error(self.client.post("/v1/analyses", content=unicode_body.encode(), headers=headers), 413, "REQUEST_TOO_LARGE")

    def test_chunked_body_cannot_evade_limit_with_a_false_content_length(self):
        captured = []
        scope = {"type": "http", "method": "POST", "path": "/v1/analyses",
                 "headers": [(b"content-length", b"1")]}
        chunks = iter([{"type": "http.request", "body": b"a" * 1500, "more_body": True},
                       {"type": "http.request", "body": b"b" * 600, "more_body": False}])

        async def receive():
            return next(chunks)

        async def send(message):
            captured.append(message)

        async def unreachable(*args):
            self.fail("The oversized body must never reach routing or persistence")

        asyncio.run(RequestBoundary(unreachable, maximum_bytes=2048, clock=lambda: self.now)(scope, receive, send))
        self.assertEqual(captured[0]["status"], 413)

    def test_ambiguous_json_encoding_and_headers_are_rejected(self):
        headers = {"Content-Type": "application/json", "Idempotency-Key": str(uuid4())}
        bodies = (b'{"video_url":"https://youtu.be/dQw4w9WgXcQ","max_comments":1,"max_comments":2}',
                  b'{"video_url":NaN}', b'\xff', b'{')
        for body in bodies:
            self.assert_error(self.client.post("/v1/analyses", content=body, headers=headers), 422, "INVALID_INPUT")
        self.assert_error(self.client.post("/v1/analyses", json=self.input.model_dump(mode="json"),
                                          headers={**headers, "Content-Encoding": "gzip"}), 422, "INVALID_INPUT")
        duplicate = [("Authorization", "Bearer fixture-alice"), ("Idempotency-Key", str(uuid4())),
                     ("Idempotency-Key", str(uuid4()))]
        self.assert_error(self.client.post("/v1/analyses", json=self.input.model_dump(mode="json"), headers=duplicate),
                          422, "INVALID_INPUT")

    def test_download_is_private_bounded_and_not_proxied(self):
        first = self.submit()
        _, work = self.jobs.owned(ALICE, first.json()["analysis_id"])
        self.complete(work, empty=False)
        path = "/v1/analyses/" + first.json()["analysis_id"] + "/result"
        with patch.object(self.s3, "get_object", side_effect=AssertionError("API must not proxy comments")):
            download = self.client.get(path)
        self.assertEqual(download.status_code, 200)
        self.assert_schema(download.json(), "ResultDownload")
        self.assertNotIn("comments", download.json())
        self.assertIn("X-Amz-Signature", download.json()["url"])
        self.assertEqual(download.headers["cache-control"], "no-store")
        self.assert_error(self.client.get(path, headers={"Authorization": "Bearer fixture-bob"}), 404, "NOT_FOUND")

    def test_owned_deletion_is_idempotent_and_other_owners_keep_access(self):
        first = self.submit()
        bob = self.submit(token="fixture-bob")
        path = "/v1/analyses/" + first.json()["analysis_id"]
        self.assertEqual(self.client.delete(path).status_code, 204)
        self.assertEqual(self.client.delete(path).content, b"")
        self.assert_error(self.client.get(path), 410, "EXPIRED")
        self.assert_error(self.client.get(path + "/result"), 410, "EXPIRED")
        self.assertEqual(self.client.get("/v1/analyses/" + bob.json()["analysis_id"],
                                         headers={"Authorization": "Bearer fixture-bob"}).status_code, 200)
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 1)

    def test_expired_owned_request_is_410_until_cleanup_then_404(self):
        first = self.submit()
        _, work = self.jobs.owned(ALICE, first.json()["analysis_id"])
        path = "/v1/analyses/" + first.json()["analysis_id"]
        self.now = work.expires_at
        self.assert_error(self.client.get(path), 410, "EXPIRED")
        self.now += timedelta(seconds=3601)
        self.reconcile.run()
        self.assert_error(self.client.get(path), 404, "NOT_FOUND")

    def test_deadline_status_fails_work_even_before_scheduled_recovery(self):
        first = self.submit()
        _, work = self.jobs.owned(ALICE, first.json()["analysis_id"])
        self.now = work.deadline_at
        status = self.client.get("/v1/analyses/" + first.json()["analysis_id"])
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["state"], "FAILED")
        self.assertEqual(self.store.get("LIMIT#GLOBAL#ACTIVE").data["count"], 0)
        self.assert_error(self.client.get("/v1/analyses/" + first.json()["analysis_id"] + "/result"), 409, "ANALYSIS_FAILED")

    def test_read_and_active_limits_return_safe_retry_information(self):
        first = self.submit()
        self.submit()
        self.assert_error(self.submit(), 429, "ACTIVE_JOB_LIMIT")
        for _ in range(30):
            self.jobs.charge_read(ALICE)
        response = self.client.get("/v1/analyses/" + first.json()["analysis_id"])
        self.assert_error(response, 429, "RATE_LIMITED")
        self.assertEqual(response.headers["retry-after"], "60")

    def test_quota_exhaustion_reports_pacific_reset_and_no_partial_admission(self):
        self.store.commit([Change("QUOTA#2026-10-12", None, {"budget": 8000, "spent": 8000, "reserved": 0})])
        response = self.submit()
        self.assert_error(response, 503, "YOUTUBE_QUOTA_EXHAUSTED")
        self.assertEqual(response.json()["error"]["reset_at"], "2026-10-13T07:00:00Z")
        self.assertTrue(response.json()["error"]["retryable"])
        self.assertIsNone(self.store.get("LIMIT#GLOBAL#ACTIVE"))

    def test_live_gate_and_unassigned_release_fail_closed(self):
        self.pipeline.fixture_mode = False
        self.assert_error(self.submit(), 503, "LIVE_ANALYSIS_DISABLED")
        self.pipeline.fixture_mode = True
        self.pipeline.settings = self.config.model_copy(update={"model": self.config.model.model_copy(update={"release_id": None})})
        self.assert_error(self.submit(), 503, "DEPENDENCY_UNAVAILABLE")
        self.assertIsNone(self.store.get("LIMIT#GLOBAL#ACTIVE"))
        with self.assertRaises(ValueError):
            APIPipeline(self.config.model_copy(update={"environment": "production"}),
                        self.jobs, self.objects, self.models, fixture_mode=True)

    def test_model_details_use_immutable_registry_and_explain_score_limits(self):
        response = self.client.get("/v1/models/current")
        self.assertEqual(response.status_code, 200)
        self.assert_schema(response.json(), "ModelDetails")
        self.assertFalse(response.json()["evaluation"]["labels_human_confirmed"])
        self.assertEqual(response.json()["confidence_definition"], CONFIDENCE_DEFINITION)
        self.assertNotIn("model_file", response.text)
        self.models.register(self.manifest)
        changed = self.manifest.model_copy(update={"details": self.manifest.details.model_copy(update={
            "evaluation": self.manifest.details.evaluation.model_copy(update={"test_accuracy": 0.99})})})
        with self.assertRaises(ValueError):
            self.models.register(changed)
        with self.assertRaises(ValueError):
            self.models.promote(self.model.release_id, expected_release_id=None)
        with self.assertRaises(ValueError):
            ReleaseManifest(code_revision="f" * 40, details=self.manifest.details)

    def test_dependency_errors_do_not_leak_exception_text_or_headers(self):
        with patch.object(self.models, "current", side_effect=RuntimeError("secret-value-not-for-response")):
            with self.assertLogs("src.Sentiment_analysis.api.middleware", level="WARNING") as captured:
                response = self.client.get("/v1/models/current", headers={"X-Request-ID": "untrusted-client-id", "Origin": ORIGIN})
        self.assert_error(response, 503, "DEPENDENCY_UNAVAILABLE")
        self.assertNotIn("secret-value-not-for-response", response.text + " ".join(captured.output))
        self.assertNotEqual(response.headers["x-request-id"], "untrusted-client-id")
        self.assertEqual(response.headers["access-control-allow-origin"], ORIGIN)

    def test_cors_uses_exact_extension_origin_and_exposes_polling_headers(self):
        allowed = self.client.options("/v1/analyses", headers={"Origin": ORIGIN,
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,idempotency-key,content-type"})
        self.assertEqual(allowed.headers["access-control-allow-origin"], ORIGIN)
        denied = self.client.options("/v1/analyses", headers={"Origin": "https://attacker.example",
                                                               "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", denied.headers)
        self.assertNotIn("access-control-allow-credentials", allowed.headers)

    def test_implemented_openapi_matches_operations_status_codes_and_parameters(self):
        implemented = self.app.openapi()
        self.assertEqual(set(implemented["paths"]), set(self.contract["paths"]))
        for path, operations in self.contract["paths"].items():
            for method, operation in operations.items():
                if method == "parameters":
                    continue
                actual = implemented["paths"][path][method]
                self.assertEqual(actual["operationId"], operation["operationId"])
                self.assertEqual(set(actual["responses"]), set(operation["responses"]), (path, method))
                self.assertEqual(actual.get("security", []), [] if path == "/health/live" else [{"CognitoAccessToken": []}])
        header = next(p for p in implemented["paths"]["/v1/analyses"]["post"]["parameters"] if p["in"] == "header")
        self.assertEqual(header["name"], "Idempotency-Key")
        self.assertTrue(header["required"])
        self.assertEqual(header["schema"]["format"], "uuid")

    def test_http_api_lambda_adapter_preserves_auth_status_headers_and_body(self):
        app = create_app(self.config, pipeline=self.pipeline, clock=lambda: self.now)
        adapter = Mangum(app, lifespan="off")
        event = {"version": "2.0", "routeKey": "POST /v1/analyses", "rawPath": "/v1/analyses",
                 "rawQueryString": "", "headers": {"authorization": "Bearer fixture-alice",
                    "content-type": "application/json", "idempotency-key": str(uuid4())},
                 "body": json.dumps(self.input.model_dump(mode="json")), "isBase64Encoded": False,
                 "requestContext": {"requestId": "gateway-fixture", "stage": "$default",
                    "http": {"method": "POST", "path": "/v1/analyses", "sourceIp": "127.0.0.1", "protocol": "HTTP/1.1"},
                    "authorizer": {"jwt": {"claims": self.claims_for("Bearer fixture-alice"),
                                           "scopes": ["analysis/read", "analysis/write"]}}}}
        response = adapter(event, SimpleNamespace(aws_request_id="lambda-fixture"))
        self.assertEqual(response["statusCode"], 202)
        self.assertFalse(response["isBase64Encoded"])
        self.assertEqual(response["headers"]["cache-control"], "no-store")
        self.assert_schema(json.loads(response["body"]), "Analysis")
        event["requestContext"].pop("authorizer")
        response = adapter(event, SimpleNamespace(aws_request_id="lambda-fixture"))
        self.assertEqual(response["statusCode"], 401)

    def test_api_import_does_not_load_training_or_contact_aws(self):
        code = """
import sys
from unittest.mock import patch
with patch('boto3.Session', side_effect=AssertionError('No AWS on import')):
    from src.Sentiment_analysis.api.lambda_handler import handler
assert not {'sklearn', 'numpy', 'pandas', 'mlflow'} & set(sys.modules)
print('offline API import passed')
"""
        process = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_wrong_method_retains_http_semantics_and_huge_length_is_safe(self):
        response = self.client.put("/health/live")
        self.assert_error(response, 405, "INVALID_INPUT")
        self.assertIn("GET", response.headers["allow"])
        response = self.client.post("/v1/analyses", content=b"{}", headers={"Content-Length": "9" * 5000})
        self.assert_error(response, 413, "REQUEST_TOO_LARGE")
