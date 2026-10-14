"""Verify Cognito and gateway trust boundaries in the synthesized template."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from infra.auth_stack import AuthStack, ROUTES
from src.Sentiment_analysis.config.auth import AuthConfigurationManager
from src.Sentiment_analysis.config.serving import ServingConfigurationManager


class AuthStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        auth = AuthConfigurationManager().get_auth_config()
        serving = ServingConfigurationManager().get_serving_config()
        app = App(outdir=cls.directory.name, context={"@aws-cdk/core:validateAgainstDefaultRules": True})
        with patch("boto3.Session", side_effect=AssertionError("No AWS lookup permitted")):
            stack = AuthStack(app, "FixtureAuth", auth=auth, api_limits=serving.api, observability=serving.observability,
                              env=Environment(region=auth.region))
            assembly = app.synth()
        cls.template = Template.from_stack(stack).to_json()
        cls.resources = cls.template["Resources"]
        manifest = json.loads((Path(assembly.directory) / "manifest.json").read_text(encoding="utf-8"))
        assert not manifest.get("missing"), "Synthesis requested an AWS context lookup"

    def resources_of(self, type):
        return [r for r in self.resources.values() if r["Type"] == type]

    def test_public_client_code_flow_callbacks_lifetimes_and_custom_scopes(self):
        client = self.resources_of("AWS::Cognito::UserPoolClient")[0]["Properties"]
        self.assertFalse(client["GenerateSecret"])
        self.assertEqual(client["AllowedOAuthFlows"], ["code"])
        self.assertEqual(set(client["AllowedOAuthScopes"]), {"openid", "analysis/read", "analysis/write"})
        self.assertEqual(client["ExplicitAuthFlows"], ["ALLOW_REFRESH_TOKEN_AUTH"])
        self.assertEqual(client["AccessTokenValidity"], 15)
        self.assertEqual(client["RefreshTokenValidity"], 7)
        self.assertTrue(client["EnableTokenRevocation"])
        callback = json.dumps(client["CallbackURLs"])
        self.assertIn("ExtensionId", callback)
        self.assertIn(".chromiumapp.org/cognito", callback)
        self.assertNotIn("*", callback)
        self.assertEqual(self.template["Parameters"]["ExtensionId"]["AllowedPattern"], "^[a-p]{32}$")
        self.assertNotIn("Default", self.template["Parameters"]["ExtensionId"])
        pool = self.resources_of("AWS::Cognito::UserPool")[0]
        self.assertEqual(pool["DeletionPolicy"], "Retain")
        self.assertEqual(pool["Properties"]["DeletionProtection"], "ACTIVE")

    def test_all_routes_have_exact_scopes_and_only_health_is_public(self):
        routes = self.resources_of("AWS::ApiGatewayV2::Route")
        self.assertEqual(len(routes), 6)
        for route in routes:
            props = route["Properties"]
            required = ROUTES[props["RouteKey"]]
            self.assertEqual(props["AuthorizationType"], "JWT" if required else "NONE")
            if required:
                self.assertEqual(props["AuthorizationScopes"], [required])
                self.assertIn("AuthorizerId", props)
            else:
                self.assertEqual(props["RouteKey"], "GET /health/live")
        authorizer = self.resources_of("AWS::ApiGatewayV2::Authorizer")[0]["Properties"]
        self.assertEqual(authorizer["AuthorizerType"], "JWT")
        self.assertEqual(authorizer["IdentitySource"], ["$request.header.Authorization"])
        self.assertEqual(len(authorizer["JwtConfiguration"]["Audience"]), 1)
        self.assertIn("cognito-idp.us-east-1.amazonaws.com", json.dumps(authorizer["JwtConfiguration"]["Issuer"]))

    def test_lambda_permissions_are_bound_to_this_api_account_and_route_methods(self):
        permissions = self.resources_of("AWS::Lambda::Permission")
        self.assertEqual(len(permissions), 6)
        for permission in permissions:
            props = permission["Properties"]
            self.assertEqual(props["Principal"], "apigateway.amazonaws.com")
            self.assertIn("SourceAccount", props)
            self.assertIn("HttpApi", json.dumps(props["SourceArn"]))
            self.assertIn("ApiFunctionName", json.dumps(props["FunctionName"]))
        self.assertEqual(self.resources_of("AWS::Lambda::Function"), [])
        self.assertEqual(self.resources_of("AWS::Lambda::Url"), [])
        self.assertEqual(self.resources_of("AWS::DynamoDB::Table"), [])

    def test_logging_throttles_cors_and_alarms_are_bounded_and_safe(self):
        stage = self.resources_of("AWS::ApiGatewayV2::Stage")[0]["Properties"]
        self.assertEqual(stage["DefaultRouteSettings"]["ThrottlingRateLimit"], 300)
        self.assertEqual(stage["RouteSettings"]["POST /v1/analyses"]["ThrottlingRateLimit"], 20)
        log_fields = json.loads(stage["AccessLogSettings"]["Format"])
        self.assertEqual(set(log_fields), {"request_id", "route", "status", "response_bytes", "integration_latency_ms"})
        self.assertEqual(self.resources_of("AWS::Logs::LogGroup")[0]["Properties"]["RetentionInDays"], 14)
        cors = self.resources_of("AWS::ApiGatewayV2::Api")[0]["Properties"]["CorsConfiguration"]
        self.assertFalse(cors["AllowCredentials"])
        self.assertEqual(len(cors["AllowOrigins"]), 1)
        self.assertNotIn("*", json.dumps(cors))
        for alarm in self.resources_of("AWS::CloudWatch::Alarm"):
            self.assertEqual(alarm["Properties"]["TreatMissingData"], "notBreaching")
        self.assertEqual(len(self.resources_of("AWS::CloudWatch::Alarm")), 4)
