"""Check deployment trust boundaries without AWS access, uploads or Docker."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from infra.serving_stack import DEVELOPMENT_CALLBACK_URLS, ServingStack, WEB_CALLBACK_URL
from src.Sentiment_analysis.config.auth import AuthConfigurationManager
from src.Sentiment_analysis.config.serving import ServingConfigurationManager


class ServingStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.settings = ServingConfigurationManager().get_serving_config()
        cls.auth = AuthConfigurationManager().get_auth_config()
        app = App(outdir=cls.directory.name, context={"@aws-cdk/core:validateAgainstDefaultRules": True})
        with patch("boto3.Session", side_effect=AssertionError("Synthesis must not contact AWS")):
            stack = ServingStack(app, "CommentLensServing-development", auth=cls.auth,
                                 settings=cls.settings, env=Environment(region=cls.settings.region))
            assembly = app.synth()
        cls.template = Template.from_stack(stack).to_json()
        cls.resources = cls.template["Resources"]
        manifest = json.loads((Path(assembly.directory) / "manifest.json").read_text(encoding="utf-8"))
        assert not manifest.get("missing"), "Synthesis cannot request AWS lookups"

    def resources_of(self, kind):
        return [resource for resource in self.resources.values() if resource["Type"] == kind]

    def policies_for(self, role):
        return [statement for resource in self.resources_of("AWS::IAM::Policy")
                if resource["Properties"]["Roles"] == [{"Ref": role}]
                for statement in resource["Properties"]["PolicyDocument"]["Statement"]]

    def test_table_matches_transactional_repository_and_has_no_ttl_shortcut(self):
        resource = self.resources_of("AWS::DynamoDB::Table")[0]
        table = resource["Properties"]
        self.assertEqual(table["BillingMode"], "PAY_PER_REQUEST")
        self.assertTrue(table["DeletionProtectionEnabled"])
        self.assertEqual(resource["DeletionPolicy"], "Retain")
        self.assertEqual(table["KeySchema"], [{"AttributeName": "pk", "KeyType": "HASH"}])
        self.assertEqual(table["StreamSpecification"]["StreamViewType"], "NEW_IMAGE")
        self.assertNotIn("TimeToLiveSpecification", table)
        expected = {"DueIndex": ("due_pk", "due_at"), "CleanupIndex": ("cleanup_pk", "cleanup_at"),
                    "OwnerIndex": ("owner_pk", "created_at")}
        for index in table["GlobalSecondaryIndexes"]:
            self.assertEqual(tuple(key["AttributeName"] for key in index["KeySchema"]), expected.pop(index["IndexName"]))
            self.assertEqual(index["Projection"], {"ProjectionType": "ALL"})
        self.assertFalse(expected)

    def test_queues_and_mappings_preserve_retry_and_lease_protocol(self):
        queue = self.resources["ReleaseQueue"]["Properties"]
        self.assertEqual(queue["VisibilityTimeout"], 6 * self.settings.worker.timeout_seconds)
        self.assertEqual(queue["RedrivePolicy"]["maxReceiveCount"], 5)
        self.assertTrue(queue["SqsManagedSseEnabled"])
        self.assertIn("ModelReleaseId", json.dumps(queue["QueueName"]))
        worker = self.resources["ReleaseQueueMapping"]["Properties"]
        self.assertEqual(worker["BatchSize"], 1)
        self.assertEqual(worker["MaximumBatchingWindowInSeconds"], 0)
        self.assertEqual(worker["FunctionResponseTypes"], ["ReportBatchItemFailures"])
        self.assertEqual(worker["ScalingConfig"]["MaximumConcurrency"], 10)
        self.assertFalse(worker["Enabled"], "Unapproved deployments cannot start the live worker")
        outbox = self.resources["OutboxStreamMapping"]["Properties"]
        self.assertEqual(outbox["FunctionResponseTypes"], ["ReportBatchItemFailures"])
        self.assertEqual(outbox["StartingPosition"], "TRIM_HORIZON")
        self.assertTrue(outbox["BisectBatchOnFunctionError"])
        pattern = json.loads(outbox["FilterCriteria"]["Filters"][0]["Pattern"])
        self.assertEqual(pattern["dynamodb"]["Keys"]["pk"]["S"], [{"prefix": "OUTBOX#"}])
        self.assertIn("DestinationConfig", outbox)
        self.assertTrue(self.resources["OutboxStreamMapping"]["DependsOn"])

    def test_only_health_is_public_and_no_function_url_or_compute_cluster_exists(self):
        for resource in self.resources_of("AWS::ApiGatewayV2::Route"):
            route = resource["Properties"]
            self.assertEqual(route["AuthorizationType"], "NONE" if route["RouteKey"] == "GET /health/live" else "JWT")
        self.assertFalse(self.resources_of("AWS::Lambda::Url"))
        self.assertFalse(any(resource["Type"].startswith(("AWS::EC2::", "AWS::ECS::")) for resource in self.resources.values()))
        self.assertNotIn("ExtensionId", self.template["Parameters"], "Web deployment cannot invent an extension ID")
        self.assertNotIn("ApiFunctionName", self.template["Parameters"], "The full stack owns its API function")
        self.assertNotIn("BootstrapVersion", self.template["Parameters"], "Direct S3 deployment does not require CDK bootstrap")
        self.assertNotIn("CheckBootstrapVersion", self.template.get("Rules", {}))

    def test_web_callbacks_and_cors_match_exact_registered_pages(self):
        client = self.resources_of("AWS::Cognito::UserPoolClient")[0]["Properties"]
        self.assertFalse(client["GenerateSecret"])
        self.assertEqual(client["AllowedOAuthFlows"], ["code"])
        self.assertEqual(client["CallbackURLs"], [WEB_CALLBACK_URL, *DEVELOPMENT_CALLBACK_URLS])
        self.assertEqual(client["LogoutURLs"], client["CallbackURLs"])
        api = self.resources_of("AWS::ApiGatewayV2::Api")[0]["Properties"]
        origins = ["https://siddharthaganguli.github.io", "http://127.0.0.1:8766", "http://localhost:8766"]
        self.assertEqual(api["CorsConfiguration"]["AllowOrigins"], origins)
        self.assertFalse(api["CorsConfiguration"]["AllowCredentials"])
        bucket = self.resources_of("AWS::S3::Bucket")[0]["Properties"]
        rule = bucket["CorsConfiguration"]["CorsRules"][0]
        self.assertEqual(rule["AllowedOrigins"], origins)
        self.assertEqual(rule["AllowedMethods"], ["GET"])
        self.assertNotIn("*", json.dumps(client["CallbackURLs"]))

    def test_objects_stay_private_encrypted_and_retained_without_touching_dvc_bucket(self):
        resource = self.resources_of("AWS::S3::Bucket")[0]
        bucket = resource["Properties"]
        self.assertTrue(all(bucket["PublicAccessBlockConfiguration"].values()))
        encryption = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
        self.assertEqual(encryption["ServerSideEncryptionByDefault"]["SSEAlgorithm"], "AES256")
        self.assertEqual(resource["DeletionPolicy"], "Retain")
        rule = bucket["LifecycleConfiguration"]["Rules"][0]
        self.assertEqual(rule["Prefix"], "serving/development/")
        self.assertEqual(rule["ExpirationInDays"], 7)
        self.assertEqual(rule["NoncurrentVersionExpiration"]["NoncurrentDays"], 7)
        self.assertNotIn("yt-comment-analyzer-main", json.dumps(bucket))
        transport = self.resources_of("AWS::S3::BucketPolicy")[0]["Properties"]["PolicyDocument"]["Statement"][0]
        self.assertEqual(transport["Effect"], "Deny")
        self.assertEqual(transport["Condition"], {"Bool": {"aws:SecureTransport": "false"}})

    def test_runtime_code_is_uploaded_and_release_overrides_are_shared(self):
        functions = self.resources_of("AWS::Lambda::Function")
        self.assertEqual(len(functions), 5)
        for resource in functions:
            function = resource["Properties"]
            self.assertEqual(function["Runtime"], "python3.12")
            self.assertEqual(function["Architectures"], ["x86_64"])
            self.assertEqual(function["Code"]["S3Bucket"], {"Ref": "RuntimeArtifactsBucket"})
            self.assertNotIn("ZipFile", function["Code"])
            self.assertEqual(function["LoggingConfig"]["LogFormat"], "Text")
            variables = function["Environment"]["Variables"]
            encoded = json.dumps(variables["SERVING_RUNTIME_OVERRIDES_JSON"])
            for name in ("ModelReleaseId", "CodeRevision", "ServingTable", "ResultsBucketResource", "ExtensionClient"):
                self.assertIn(name, encoded)
            self.assertIn('\\"live_enabled\\":false', encoded)
        self.assertEqual(self.resources["ApiFunction"]["Properties"]["ReservedConcurrentExecutions"], 10)
        self.assertIn("SERVING_QUEUE_REGISTRY_JSON", self.resources["DispatcherFunction"]["Properties"]["Environment"]["Variables"])
        self.assertIn("SERVING_IMAGE_CODE_REVISION", self.resources["WorkerFunction"]["Properties"]["Environment"]["Variables"])

    def test_iam_transactions_secret_access_and_cleanup_are_scoped(self):
        api = self.policies_for("ApiRole")
        worker = self.policies_for("WorkerRole")
        maintenance = self.policies_for("ReconcilerRole")
        for role in ("ApiRole", "WorkerRole", "DispatcherRole", "ReconcilerRole"):
            policies = self.policies_for(role)
            writes = [s for s in policies if "dynamodb:PutItem" in s["Action"]]
            self.assertEqual(len(writes), 1)
            self.assertEqual(set(writes[0]["Action"]), {"dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"})
            self.assertEqual(writes[0]["Condition"], {"ForAnyValue:StringEquals": {"dynamodb:EnclosingOperation": ["TransactWriteItems"]}})
            self.assertEqual(writes[0]["Resource"], [{"Fn::GetAtt": ["ServingTable", "Arn"]}])
        self.assertFalse(any("secretsmanager:GetSecretValue" in s["Action"] for s in api + maintenance))
        secret = next(s for s in worker if "secretsmanager:GetSecretValue" in s["Action"])
        self.assertEqual(secret["Resource"], [self.settings.youtube.api_key_secret_arn])
        self.assertFalse(any("sqs:SendMessage" in s["Action"] for s in api + worker))
        listing = next(s for s in maintenance if "s3:ListBucketVersions" in s["Action"])
        self.assertEqual(listing["Condition"], {"StringLike": {"s3:prefix": ["serving/development/work/*"]}})
        for resource in self.resources_of("AWS::IAM::Policy"):
            for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
                if "*" in statement["Resource"]:
                    self.assertEqual(statement["Action"], ["dynamodb:ListStreams"])
                    self.assertEqual(statement["Condition"], {"StringEquals": {"aws:RequestedRegion": "us-east-1"}})
                self.assertFalse(any(action.endswith(":*") for action in statement["Action"]))

    def test_verifier_loads_worker_package_with_logs_only_permissions(self):
        verifier = self.resources["VerifierFunction"]["Properties"]
        self.assertEqual(verifier["Handler"], "src.Sentiment_analysis.workers.verify_release.handler")
        self.assertEqual(verifier["Code"]["S3Key"], {"Ref": "WorkerCodeKey"})
        self.assertEqual(verifier["ReservedConcurrentExecutions"], 1)
        self.assertEqual(verifier["Timeout"], 60)
        actions = {action for statement in self.policies_for("VerifierRole") for action in statement["Action"]}
        self.assertEqual(actions, {"logs:CreateLogStream", "logs:PutLogEvents"})
        self.assertIn("VerifierFunctionName", self.template["Outputs"])

    def test_every_runtime_role_requires_the_admin_created_permissions_boundary(self):
        roles = self.resources_of("AWS::IAM::Role")
        self.assertEqual(len(roles), 5)
        for resource in roles:
            boundary = json.dumps(resource["Properties"]["PermissionsBoundary"])
            self.assertIn("AWS::AccountId", boundary)
            self.assertIn("CommentLensServingDevelopmentRuntimeBoundary", boundary)
        self.assertEqual(self.template["Metadata"]["CommentLensReleaseWorkflow"], "first-development-release")
        self.assertFalse(self.resources_of("AWS::IAM::ManagedPolicy"), "An operator cannot create or widen its own boundary")

    def test_minute_recovery_has_exact_invocation_permission(self):
        rule = self.resources["MaintenanceSchedule"]["Properties"]
        self.assertEqual(rule["ScheduleExpression"], "rate(1 minute)")
        self.assertEqual(rule["Targets"][0]["Arn"], {"Fn::GetAtt": ["ReconcilerFunction", "Arn"]})
        permission = self.resources["AllowScheduledMaintenance"]["Properties"]
        self.assertEqual(permission["Principal"], "events.amazonaws.com")
        self.assertEqual(permission["SourceArn"], {"Fn::GetAtt": ["MaintenanceSchedule", "Arn"]})
        self.assertIn("SourceAccount", permission)

    def test_live_synthesis_requires_approval_reference_and_explicit_enable(self):
        with tempfile.TemporaryDirectory() as directory:
            app = App(outdir=directory)
            stack = ServingStack(app, "CommentLensLiveTest", auth=self.auth, settings=self.settings,
                                 live_youtube=True, env=Environment(region=self.settings.region))
            template = Template.from_stack(stack).to_json()
        self.assertTrue(template["Resources"]["ReleaseQueueMapping"]["Properties"]["Enabled"])
        self.assertIn("RequireAcceptedApproval", template["Rules"])
        assertion = template["Rules"]["RequireAcceptedApproval"]["Assertions"][0]["Assert"]
        self.assertEqual(assertion, {"Fn::Not": [{"Fn::Equals": [{"Ref": "AnalyticsUseApprovalReference"}, ""]}]})

    def test_callback_configuration_rejects_untrusted_shapes(self):
        invalid = ("https://example.com/dashboard.html?redirect=evil", "https://example.com/dashboard.html#token",
                   "https://user:password@example.com/dashboard.html", "http://example.com/dashboard.html",
                   "https://example.com/a/../dashboard.html", "https://example.com/%2e/dashboard.html")
        for index, url in enumerate(invalid):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                app = App(outdir=directory)
                with self.assertRaises(ValueError):
                    ServingStack(app, f"BadCallback{index}", auth=self.auth, settings=self.settings,
                                 web_callback_url=url, env=Environment(region=self.settings.region))
