"""Deploy the first development serving release, verify it, then emit web settings.

Uses the normal AWS profile/role chain. This command never reads DVC credentials
or a YouTube key. CloudFormation gives the worker its own scoped execution role.
An existing different release requires a separate drain/promotion workflow.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
STACK_NAME = "CommentLensServing-development"
ARTIFACT_BUCKET = "yt-comment-analyzer-main"


def checked_release(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("dependencies_installed") is not True:
        raise ValueError("Use a complete packaged Lambda release")
    from src.Sentiment_analysis.entity.analysis import ModelReference
    from src.Sentiment_analysis.entity.api import release_fingerprint
    from src.Sentiment_analysis.config.serving import ServingConfigurationManager
    settings = ServingConfigurationManager().get_serving_config()
    model = ModelReference(release_id=manifest["release_id"], sha256=settings.model.sha256,
        preprocessing_sha256=settings.model.preprocessing_sha256,
        review_confidence_threshold=settings.worker.review_confidence_threshold,
        result_schema_version=settings.model.result_schema_version)
    if (manifest.get("model_sha256") != settings.model.sha256
            or release_fingerprint(model, manifest["code_revision"]) != manifest["release_id"]):
        raise ValueError("Release fingerprint differs from configured model/code")
    for name in ("api", "worker"):
        package = manifest["packages"][name]
        if package["file"] != f"{name}.zip" or not 0 < package["uncompressed_bytes"] <= 250 * 1024 * 1024:
            raise ValueError("Invalid Lambda package metadata")
        with (directory / package["file"]).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != package["sha256"]:
                raise ValueError("Lambda package changed after packaging")
    return manifest, settings, model


def release_manifest(settings, model, revision):
    from src.Sentiment_analysis.entity.api import ReleaseManifest
    report = json.loads((ROOT / "artifacts/model_evaluation/evaluation_report.json").read_text(encoding="utf-8"))
    if (report.get("passed") is not True or report.get("model_sha256") != model.sha256
            or report.get("preprocessing_sha256") != model.preprocessing_sha256):
        raise ValueError("Evaluation report does not match the packaged release")
    return ReleaseManifest.model_validate({"code_revision": revision, "details": {
        "model": model.model_dump(mode="json"), "type": "TF-IDF + LogisticRegression",
        "labels": {"0": "Negative", "1": "Neutral", "2": "Positive"},
        "confidence_definition": "Highest class score, not calibrated correctness probability",
        "evaluation": {"test_accuracy": report["test_metrics"]["accuracy"],
            "test_macro_f1": report["test_metrics"]["f1_macro"], "test_rows": report["test_rows"],
            "labels_human_confirmed": report["labels_manually_verified"],
            "context": "Agreement with supplied dataset labels on held-out video-title groups; not accuracy on this video."}}})


def web_settings(outputs, redirect_uri):
    return {"enabled": True, "apiBaseUrl": outputs["ApiUrl"], "cognitoDomain": outputs["CognitoDomain"],
            "clientId": outputs["PublicClientId"], "redirectUri": redirect_uri,
            "resultBaseUrl": outputs["ResultBaseUrl"], "pollScheduleSeconds": [2, 5, 10], "maxComments": 1000}


def deploy_release(session, directory, template_path, *, approval_reference=""):
    from botocore.exceptions import ClientError
    from src.Sentiment_analysis.repositories.dynamo import DynamoStore
    from src.Sentiment_analysis.repositories.models import ModelRepository

    manifest, settings, model = checked_release(directory)
    directory = Path(directory).resolve()
    template_path = Path(template_path).resolve()
    template = json.loads(template_path.read_text(encoding="utf-8"))
    if template.get("Metadata", {}).get("CommentLensReleaseWorkflow") != "first-development-release":
        raise ValueError("Use the serving template emitted by infra.serving_app")
    cfn, s3 = session.client("cloudformation"), session.client("s3")
    prefix = f"serving-deployments/development/{manifest['release_id']}"
    code_keys = {name: f"{prefix}/{name}-{manifest['packages'][name]['sha256']}.zip" for name in ("api", "worker")}
    existing = None
    try:
        existing = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ValidationError":
            raise
    if existing:
        previous = {row["OutputKey"]: row["OutputValue"] for row in existing.get("Outputs", [])}
        if previous.get("ServingReleaseId") != manifest["release_id"]:
            raise ValueError("An existing different release needs drain/promotion; refusing to replace active workers")
        parameters = {row["ParameterKey"]: row.get("ParameterValue") for row in existing.get("Parameters", [])}
        if parameters.get("ApiCodeKey") != code_keys["api"] or parameters.get("WorkerCodeKey") != code_keys["worker"]:
            raise ValueError("This release already has different immutable packages; reuse its original ZIPs")
    block = s3.get_public_access_block(Bucket=ARTIFACT_BUCKET)["PublicAccessBlockConfiguration"]
    if not all(block.get(field) is True for field in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
        raise ValueError("Deployment packages require a bucket with public access blocked")
    for name in ("api", "worker"):
        s3.upload_file(str(directory / f"{name}.zip"), ARTIFACT_BUCKET, code_keys[name],
                       ExtraArgs={"ServerSideEncryption": "AES256", "ChecksumAlgorithm": "SHA256"})
    template_hash = hashlib.sha256(template_path.read_bytes()).hexdigest()
    template_key = f"{prefix}/serving-{template_hash}.template.json"
    s3.put_object(Bucket=ARTIFACT_BUCKET, Key=template_key, Body=template_path.read_bytes(),
                  ServerSideEncryption="AES256", ContentType="application/json")
    parameters = {"RuntimeArtifactsBucket": ARTIFACT_BUCKET, "ApiCodeKey": code_keys["api"],
        "WorkerCodeKey": code_keys["worker"], "CodeRevision": manifest["code_revision"],
        "ModelReleaseId": manifest["release_id"], "AnalyticsUseApprovalReference": approval_reference}
    request = {"StackName": STACK_NAME, "TemplateURL": f"https://{ARTIFACT_BUCKET}.s3.amazonaws.com/{template_key}",
        "Parameters": [{"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()],
        "Capabilities": ["CAPABILITY_NAMED_IAM"], "ClientRequestToken": str(uuid4()),
        "Tags": [{"Key": "Project", "Value": "CommentLens"}, {"Key": "Environment", "Value": "development"}]}
    changed = True
    if existing:
        try:
            cfn.update_stack(**request)
        except ClientError as error:
            if error.response["Error"]["Code"] == "ValidationError" and "No updates" in error.response["Error"].get("Message", ""):
                changed = False
            else:
                raise
        waiter = "stack_update_complete"
    else:
        cfn.create_stack(**request, EnableTerminationProtection=True)
        waiter = "stack_create_complete"
    if changed:
        cfn.get_waiter(waiter).wait(StackName=STACK_NAME, WaiterConfig={"Delay": 10, "MaxAttempts": 120})
    outputs = {row["OutputKey"]: row["OutputValue"] for row in cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]["Outputs"]}
    verification = session.client("lambda").invoke(FunctionName=outputs["VerifierFunctionName"],
        InvocationType="RequestResponse", Payload=b"{}")
    verified = json.loads(verification["Payload"].read(8192))
    if verification.get("FunctionError") or verified.get("verified") is not True or verified.get("release_id") != manifest["release_id"]:
        raise ValueError("Linux model verification failed; the release was not promoted")
    store = DynamoStore(session.client("dynamodb"), outputs["TableName"])
    models = ModelRepository(store)
    models.register(release_manifest(settings, model, manifest["code_revision"]))
    current = store.get("MODEL#CURRENT")
    if current is None:
        models.promote(manifest["release_id"], expected_release_id=None)
    elif current.data["release_id"] != manifest["release_id"]:
        raise ValueError("Current release changed; no website settings were published")
    public = web_settings(outputs, outputs["WebCallbackUrl"])
    (ROOT / "config/web-client.public.json").write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")
    local = web_settings(outputs, "http://127.0.0.1:8766/dashboard.html")
    (directory / "web-client.local.json").write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")
    (directory / "stack-outputs.json").write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
    print("Serving release verified and registered. Public website settings generated; commit and push them to publish.")
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--profile", help="Existing local AWS profile; otherwise use the default role/profile chain")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--deploy", action="store_true", help="Create/update the named development stack in AWS")
    args = parser.parse_args()
    manifest, _, _ = checked_release(args.release)
    if not args.deploy:
        print(f"Release {manifest['release_id']} validated. --deploy creates the development stack, verifies the model and emits web settings.")
        return
    import boto3
    deploy_release(boto3.Session(profile_name=args.profile, region_name="us-east-1"), args.release, args.template,
                   approval_reference=args.approval_reference)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    try:
        main()
    except Exception as error:
        from botocore.exceptions import ClientError
        detail = error.response["Error"]["Code"] if isinstance(error, ClientError) else str(error) if isinstance(error, ValueError) else type(error).__name__
        raise SystemExit(f"Serving deployment stopped: {detail}") from None
