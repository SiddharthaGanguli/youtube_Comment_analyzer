"""Synthesize only. Run from the repository root; no SDK lookup is required."""

import argparse

from aws_cdk import App, Environment

from infra.auth_stack import AuthStack
from src.Sentiment_analysis.config.auth import AuthConfigurationManager
from src.Sentiment_analysis.config.serving import ServingConfigurationManager


def main():
    parser = argparse.ArgumentParser(description="Synthesize Cognito/API authorization; create no AWS resources")
    parser.add_argument("--config", default="config/auth-infrastructure.yaml")
    parser.add_argument("--serving-config", default="config/serving.yaml")
    parser.add_argument("--outdir", default="artifacts/auth-infrastructure")
    args = parser.parse_args()
    auth = AuthConfigurationManager(args.config).get_auth_config()
    serving = ServingConfigurationManager(args.serving_config).get_serving_config()
    if serving.region != auth.region:
        raise ValueError("Auth and serving regions must match")
    app = App(outdir=args.outdir, context={"@aws-cdk/core:validateAgainstDefaultRules": True})
    AuthStack(app, f"CommentLensAuth-{auth.environment}", auth=auth, api_limits=serving.api,
              observability=serving.observability, env=Environment(region=auth.region))
    app.synth()
    print("Auth template synthesized. ExtensionId and ApiFunctionName are required at deployment.")


if __name__ == "__main__":
    main()
