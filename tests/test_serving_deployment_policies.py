"""Keep attachable deployment policies and bounded runtime roles deployable."""

import json
from pathlib import Path
import unittest


CONFIG = Path(__file__).resolve().parents[1] / "config"
BOUNDARY = "arn:aws:iam::895812756022:policy/CommentLensServingDevelopmentRuntimeBoundary"
FILES = ("aws-serving-deployment-policy.json", "aws-serving-web-deployment-policy.json",
         "aws-serving-runtime-boundary.json")


def actions(statement):
    value = statement["Action"]
    return [value] if isinstance(value, str) else value


class ServingDeploymentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policies = {name: json.loads((CONFIG / name).read_text(encoding="utf-8")) for name in FILES}

    def test_each_document_fits_the_customer_managed_policy_limit(self):
        for name, document in self.policies.items():
            with self.subTest(policy=name):
                self.assertEqual(document["Version"], "2012-10-17")
                self.assertLessEqual(len(json.dumps(document, separators=(",", ":"))), 6144)
                self.assertTrue(document["Statement"])
                self.assertTrue(all(item["Effect"] == "Allow" for item in document["Statement"]))

    def test_delegated_role_writes_cannot_create_or_update_an_unbounded_role(self):
        statements = self.policies[FILES[0]]["Statement"]
        for action in ("iam:CreateRole", "iam:PutRolePolicy", "iam:UpdateAssumeRolePolicy"):
            matching = [item for item in statements if action in actions(item)]
            self.assertTrue(matching, action)
            for item in matching:
                self.assertEqual(item["Condition"]["StringEquals"]["iam:PermissionsBoundary"], BOUNDARY)
                self.assertEqual(item["Resource"], "arn:aws:iam::895812756022:role/CommentLensServing-development-*-role")
        allowed = {action for document in self.policies.values() for item in document["Statement"] for action in actions(item)}
        self.assertFalse(allowed & {"iam:AttachRolePolicy", "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary",
                                    "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:CreateAccessKey"})
        passing = next(item for item in statements if "iam:PassRole" in actions(item))
        self.assertEqual(passing["Condition"]["StringEquals"]["iam:PassedToService"], "lambda.amazonaws.com")

    def test_runtime_boundary_contains_no_control_plane_or_other_project_data(self):
        statements = self.policies[FILES[2]]["Statement"]
        for item in statements:
            for action in actions(item):
                self.assertIn(action.split(":", 1)[0], {"logs", "dynamodb", "sqs", "s3", "secretsmanager"})
                self.assertNotIn("*", action)
            resources = [item["Resource"]] if isinstance(item["Resource"], str) else item["Resource"]
            if "*" in resources:
                self.assertEqual(actions(item), ["dynamodb:ListStreams"])
                self.assertEqual(item["Condition"]["StringEquals"]["aws:RequestedRegion"], "us-east-1")
            for resource in resources:
                if resource == "*":
                    continue
                self.assertNotIn("yt-comment-analyzer-main", resource)
                self.assertTrue(any(project in resource for project in (
                    "CommentLensServing-development-", "comment-lens-development-serving",
                    "comment-lens-development-895812756022-us-east-1-results", "cl-dev-sr_",
                    "comment-lens/youtube-api-key-1P4SMe")), resource)
        writes = next(item for item in statements if "dynamodb:PutItem" in actions(item))
        self.assertEqual(writes["Condition"]["ForAnyValue:StringEquals"]["dynamodb:EnclosingOperation"], ["TransactWriteItems"])
        version_listing = next(item for item in statements if "s3:ListBucketVersions" in actions(item))
        self.assertEqual(version_listing["Condition"]["StringLike"]["s3:prefix"], ["serving/development/work/*"])

    def test_mapping_writes_are_function_scoped_and_required_global_reads_are_regional(self):
        core = self.policies[FILES[0]]["Statement"]
        for action in ("lambda:CreateEventSourceMapping", "lambda:UpdateEventSourceMapping", "lambda:DeleteEventSourceMapping"):
            item = next(item for item in core if action in actions(item))
            self.assertEqual(item["Condition"]["ArnLike"]["lambda:FunctionArn"],
                             "arn:aws:lambda:us-east-1:895812756022:function:CommentLensServing-development-*")
        web = self.policies[FILES[1]]["Statement"]
        domain = next(item for item in web if "cognito-idp:DescribeUserPoolDomain" in actions(item))
        self.assertEqual(domain["Resource"], "*")
        self.assertEqual(domain["Condition"]["StringEquals"]["aws:RequestedRegion"], "us-east-1")
        log_delivery = next(item for item in web if "logs:PutResourcePolicy" in actions(item))
        self.assertEqual(log_delivery["Resource"], "*")
        self.assertEqual(log_delivery["Condition"]["StringEquals"]["aws:RequestedRegion"], "us-east-1")
        service_linked = next(item for item in web if "iam:CreateServiceLinkedRole" in actions(item))
        self.assertEqual(service_linked["Condition"]["StringEquals"]["iam:AWSServiceName"], "ops.apigateway.amazonaws.com")
        self.assertTrue(service_linked["Resource"].endswith("/ops.apigateway.amazonaws.com/AWSServiceRoleForAPIGateway"))


if __name__ == "__main__":
    unittest.main()
