"""Cognito, scoped HTTP API and alarms around an existing API Lambda.

There is no model worker, serving table or queue deployment in this stack.
"""

import json
import re
from urllib.parse import urlsplit

from aws_cdk import Aws, CfnOutput, CfnParameter, Fn, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as gateway, aws_cloudwatch as cloudwatch
from aws_cdk import aws_cognito as cognito, aws_lambda as lambda_, aws_logs as logs
from constructs import Construct

from src.Sentiment_analysis.entity.serving import validate_origin

ROUTES = {
    "POST /v1/analyses": "analysis/write",
    "GET /v1/analyses/{analysis_id}": "analysis/read",
    "GET /v1/analyses/{analysis_id}/result": "analysis/read",
    "DELETE /v1/analyses/{analysis_id}": "analysis/write",
    "GET /v1/models/current": "analysis/read",
    "GET /health/live": None,
}


class AuthStack(Stack):
    def __init__(self, scope: Construct, id: str, *, auth, api_limits, observability,
                 defer_authorization=False, **kwargs):
        super().__init__(scope, id, **kwargs)
        if not defer_authorization:
            self.configure_authorization(auth=auth, api_limits=api_limits, observability=observability)

    def configure_authorization(self, *, auth, api_limits, observability, function_arn=None,
                                include_extension=True, extension_id=None, web_callback_urls=()):
        """Reuse the same JWT boundary for the extension and exact web callbacks."""
        callbacks, logouts, origins = [], [], []
        extension_origin = callback = logout = None
        if include_extension:
            if extension_id is None:
                extension = CfnParameter(self, "ExtensionId", type="String", allowed_pattern="^[a-p]{32}$",
                    description="Real Chrome extension ID; no fixture/default ID is accepted")
                extension_id = extension.value_as_string
            else:
                if not re.fullmatch(r"[a-p]{32}", extension_id):
                    raise ValueError("Configure the real Chrome extension ID")
            callback = Fn.join("", ["https://", extension_id, ".chromiumapp.org/", auth.callback_path])
            logout = Fn.join("", ["https://", extension_id, ".chromiumapp.org/", auth.logout_path])
            extension_origin = Fn.join("", ["chrome-extension://", extension_id])
            callbacks.append(callback)
            logouts.append(logout)
            origins.append(extension_origin)
        elif extension_id is not None:
            raise ValueError("An extension ID requires extension authorization")
        if len(set(web_callback_urls)) != len(web_callback_urls):
            raise ValueError("Web callbacks must be unique")
        for url in web_callback_urls:
            if not isinstance(url, str) or len(url) > 1024:
                raise ValueError("Web callbacks must be exact URLs")
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}"
            validate_origin(origin, allow_local=auth.environment == "development")
            if (parts.scheme not in {"http", "https"} or not parts.path.startswith("/")
                    or parts.query or parts.fragment or "%" in url or "\\" in url
                    or not re.fullmatch(r"[A-Za-z0-9.-]+", parts.hostname or "")
                    or not re.fullmatch(r"/[A-Za-z0-9_./-]*", parts.path)
                    or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url)
                    or any(segment in {".", ".."} for segment in parts.path.split("/"))):
                raise ValueError("Web callbacks cannot contain credentials, traversal, queries or fragments")
            callbacks.append(url)
            logouts.append(url)
            if origin not in origins:
                origins.append(origin)
        if not callbacks:
            raise ValueError("At least one registered callback is required")
        if function_arn is None:
            function = CfnParameter(self, "ApiFunctionName", type="String", allowed_pattern=r"^[A-Za-z0-9_-]{1,64}(?::[A-Za-z0-9_-]{1,128})?$",
                description="Existing API Lambda name[:alias], in this account/region, running the Mangum handler")
            function_arn = Fn.join("", ["arn:", Aws.PARTITION, ":lambda:", Aws.REGION, ":", Aws.ACCOUNT_ID,
                                        ":function:", function.value_as_string])
        pool = cognito.CfnUserPool(self, "UserPool", username_attributes=["email"], auto_verified_attributes=["email"],
            username_configuration=cognito.CfnUserPool.UsernameConfigurationProperty(case_sensitive=False),
            admin_create_user_config=cognito.CfnUserPool.AdminCreateUserConfigProperty(allow_admin_create_user_only=not auth.self_signup_enabled),
            account_recovery_setting=cognito.CfnUserPool.AccountRecoverySettingProperty(recovery_mechanisms=[
                cognito.CfnUserPool.RecoveryOptionProperty(name="verified_email", priority=1)]),
            policies=cognito.CfnUserPool.PoliciesProperty(password_policy=cognito.CfnUserPool.PasswordPolicyProperty(
                minimum_length=12, require_lowercase=True, require_uppercase=True, require_numbers=True, require_symbols=True)),
            deletion_protection="ACTIVE", mfa_configuration="OFF")
        pool.apply_removal_policy(RemovalPolicy.RETAIN)
        resource = cognito.CfnUserPoolResourceServer(self, "AnalysisScopes", identifier="analysis", name="Comment Lens analyses",
            user_pool_id=pool.ref, scopes=[cognito.CfnUserPoolResourceServer.ResourceServerScopeTypeProperty(
                scope_name=name, scope_description=description) for name, description in (
                ("read", "Read owned analysis results and model metadata"), ("write", "Submit analyses and remove owned references"))])
        client = cognito.CfnUserPoolClient(self, "ExtensionClient", user_pool_id=pool.ref, generate_secret=False,
            allowed_o_auth_flows=["code"], allowed_o_auth_flows_user_pool_client=True,
            allowed_o_auth_scopes=["openid", "analysis/read", "analysis/write"],
            callback_ur_ls=callbacks, logout_ur_ls=logouts, supported_identity_providers=["COGNITO"],
            explicit_auth_flows=["ALLOW_REFRESH_TOKEN_AUTH"], prevent_user_existence_errors="ENABLED",
            enable_token_revocation=True, access_token_validity=auth.access_token_minutes,
            id_token_validity=auth.access_token_minutes, refresh_token_validity=auth.refresh_token_days,
            token_validity_units=cognito.CfnUserPoolClient.TokenValidityUnitsProperty(access_token="minutes", id_token="minutes", refresh_token="days"))
        client.add_resource_dependency(resource)
        domain_name = Fn.join("-", [auth.cognito_domain_prefix, Aws.ACCOUNT_ID])
        # Classic hosted UI does not require a separately provisioned branding style.
        domain = cognito.CfnUserPoolDomain(self, "LoginDomain", domain=domain_name, user_pool_id=pool.ref,
                                          managed_login_version=1)
        issuer = Fn.join("", ["https://cognito-idp.", auth.region, ".amazonaws.com/", pool.ref])
        login_origin = Fn.join("", ["https://", domain.ref, ".auth.", auth.region, ".amazoncognito.com"])
        api = gateway.CfnApi(self, "HttpApi", protocol_type="HTTP", name=f"comment-lens-{auth.environment}",
            cors_configuration=gateway.CfnApi.CorsProperty(allow_origins=origins, allow_credentials=False,
                allow_headers=["Authorization", "Content-Type", "Idempotency-Key"], allow_methods=["GET", "POST", "DELETE"],
                expose_headers=["Location", "Retry-After", "X-Request-ID"], max_age=600))
        authorizer = gateway.CfnAuthorizer(self, "AccessTokenAuthorizer", api_id=api.ref, authorizer_type="JWT",
            name="CognitoAccessToken", identity_source=["$request.header.Authorization"],
            jwt_configuration=gateway.CfnAuthorizer.JWTConfigurationProperty(audience=[client.ref], issuer=issuer))
        integration = gateway.CfnIntegration(self, "ApiIntegration", api_id=api.ref, integration_type="AWS_PROXY",
            integration_uri=function_arn, integration_method="POST", payload_format_version="2.0",
            timeout_in_millis=api_limits.timeout_seconds * 1000)
        configured_routes = {}
        for index, (route, required_scope) in enumerate(ROUTES.items()):
            configured_routes[route] = gateway.CfnRoute(self, f"Route{index}", api_id=api.ref, route_key=route,
                target=Fn.join("/", ["integrations", integration.ref]), authorization_type="JWT" if required_scope else "NONE",
                authorizer_id=authorizer.ref if required_scope else None,
                authorization_scopes=[required_scope] if required_scope else None)
            method, path = route.split(" ", 1)
            execution_path = path.replace("{analysis_id}", "*")
            source = Fn.join("", ["arn:", Aws.PARTITION, ":execute-api:", Aws.REGION, ":", Aws.ACCOUNT_ID,
                                   ":", api.ref, "/*/", method, execution_path])
            lambda_.CfnPermission(self, f"InvokeRoute{index}", action="lambda:InvokeFunction",
                function_name=function_arn, principal="apigateway.amazonaws.com",
                source_account=Aws.ACCOUNT_ID, source_arn=source)
        access_log = logs.CfnLogGroup(self, "HttpAccessLog", retention_in_days=observability.log_retention_days)
        # Never include headers, URL/query, IP, subject, authorizer claims or error text.
        access_format = json.dumps({"request_id": "$context.requestId", "route": "$context.routeKey",
                                    "status": "$context.status", "response_bytes": "$context.responseLength",
                                    "integration_latency_ms": "$context.integrationLatency"}, separators=(",", ":"))
        stage = gateway.CfnStage(self, "DefaultStage", api_id=api.ref, stage_name="$default", auto_deploy=True,
            access_log_settings=gateway.CfnStage.AccessLogSettingsProperty(destination_arn=access_log.attr_arn, format=access_format),
            default_route_settings=gateway.CfnStage.RouteSettingsProperty(detailed_metrics_enabled=True,
                throttling_rate_limit=api_limits.throttle_requests_per_second, throttling_burst_limit=api_limits.throttle_burst),
            route_settings={"POST /v1/analyses": {"DetailedMetricsEnabled": True,
                "ThrottlingRateLimit": api_limits.submission_requests_per_second,
                "ThrottlingBurstLimit": api_limits.submission_burst}})
        # RouteSettings contains a string key, so CloudFormation cannot infer
        # that this route must exist before it creates the stage.
        stage.add_resource_dependency(configured_routes["POST /v1/analyses"])
        for name, metric, service, threshold in (
                ("DependencyFailures", "DependencyFailureCount", "api", 5),
                ("QuotaDenied", "QuotaDeniedCount", "jobs", 1),
                ("ProviderCircuit", "ProviderCircuitOpenedCount", "jobs", 1),
                ("WorkerFailures", "JobFailedCount", "jobs", 5)):
            cloudwatch.CfnAlarm(self, name, namespace="CommentLens/Serving", metric_name=metric, statistic="Sum",
                dimensions=[cloudwatch.CfnAlarm.DimensionProperty(name="Environment", value=auth.environment),
                            cloudwatch.CfnAlarm.DimensionProperty(name="Service", value=service)],
                period=300, evaluation_periods=1, threshold=threshold, comparison_operator="GreaterThanOrEqualToThreshold",
                treat_missing_data="notBreaching", alarm_description=f"Comment Lens {name}; notification routing is configured at deployment")
        cloudwatch.CfnAlarm(self, "GatewayFailures", namespace="AWS/ApiGateway", metric_name="5xx", statistic="Sum",
            dimensions=[cloudwatch.CfnAlarm.DimensionProperty(name="ApiId", value=api.ref)], period=300,
            evaluation_periods=1, threshold=5, comparison_operator="GreaterThanOrEqualToThreshold", treat_missing_data="notBreaching")
        outputs = {"UserPoolId": pool.ref, "PublicClientId": client.ref, "CognitoIssuer": issuer,
                "CognitoDomain": login_origin, "AuthorizationEndpoint": Fn.join("", [login_origin, "/oauth2/authorize"]),
                "TokenEndpoint": Fn.join("", [login_origin, "/oauth2/token"]),
                "LogoutEndpoint": Fn.join("", [login_origin, "/logout"]), "ApiUrl": api.attr_api_endpoint}
        if include_extension:
            outputs.update(CallbackUrl=callback, LogoutCallbackUrl=logout, ExtensionOrigin=extension_origin)
        if web_callback_urls:
            outputs["WebCallbackUrl"] = web_callback_urls[0]
        for name, value in outputs.items():
            CfnOutput(self, name, value=value)
        self.authorization = {"pool": pool, "client": client, "api": api, "issuer": issuer,
                              "domain": login_origin, "origins": origins, "extension_id": extension_id}
