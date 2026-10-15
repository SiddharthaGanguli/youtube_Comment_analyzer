"""Synthesize the complete serving template. This command creates no resources."""

import argparse

from aws_cdk import App, Environment

from infra.serving_stack import ServingStack, WEB_CALLBACK_URL
from src.Sentiment_analysis.config.auth import AuthConfigurationManager
from src.Sentiment_analysis.config.serving import ServingConfigurationManager


def main():
    parser = argparse.ArgumentParser(description="Synthesize the serverless backend without AWS calls")
    parser.add_argument("--config", default="config/serving.yaml")
    parser.add_argument("--auth-config", default="config/auth-infrastructure.yaml")
    parser.add_argument("--outdir", default="artifacts/serving-infrastructure")
    parser.add_argument("--stack-name", default=None)
    parser.add_argument("--web-callback-url", default=WEB_CALLBACK_URL)
    parser.add_argument("--extension-id", default=None, help="Optional real Chrome extension ID")
    parser.add_argument("--enable-live-youtube", action="store_true",
                        help="Require the accepted approval reference at deployment; never substitutes for approval")
    args = parser.parse_args()
    settings = ServingConfigurationManager(args.config).get_serving_config()
    auth = AuthConfigurationManager(args.auth_config).get_auth_config()
    app = App(outdir=args.outdir, context={"@aws-cdk/core:validateAgainstDefaultRules": True})
    ServingStack(app, args.stack_name or f"CommentLensServing-{settings.environment}", auth=auth, settings=settings,
        live_youtube=args.enable_live_youtube, web_callback_url=args.web_callback_url, extension_id=args.extension_id,
        env=Environment(region=settings.region))
    assembly = app.synth()
    print(f"Serving template synthesized in {assembly.directory}; no AWS resources created.")
    print("Upload verified Linux runtime ZIPs and supply their bucket/keys, code revision and model release at deployment.")
    print(f"Live YouTube mapping: {'enabled only with accepted approval reference' if args.enable_live_youtube else 'disabled'}.")


if __name__ == "__main__":
    main()
