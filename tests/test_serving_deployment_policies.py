"""Keep attachable deployment policies and bounded runtime roles deployable."""

import fnmatch
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
        # DescribeIndexPolicies has no resource-level permission in IAM.
        index_policies = next(item for item in web if "logs:DescribeIndexPolicies" in actions(item))
        self.assertEqual(index_policies["Resource"], "*")
        self.assertEqual(index_policies["Condition"]["StringEquals"]["aws:RequestedRegion"], "us-east-1")
        service_linked = next(item for item in web if "iam:CreateServiceLinkedRole" in actions(item))
        self.assertEqual(service_linked["Condition"]["StringEquals"]["iam:AWSServiceName"], "ops.apigateway.amazonaws.com")
        self.assertTrue(service_linked["Resource"].endswith("/ops.apigateway.amazonaws.com/AWSServiceRoleForAPIGateway"))

    def test_provider_stabilization_permissions_stay_with_the_project_resource(self):
        # Current us-east-1 CloudFormation provider schemas declare these reads
        # separately from create, including metadata for unset optional features.
        # Omitting them can leave a successful service create unable to stabilize.
        statements = [item for name in FILES[:2] for item in self.policies[name]["Statement"]]
        bucket = "arn:aws:s3:::comment-lens-development-895812756022-us-east-1-results"
        table = "arn:aws:dynamodb:us-east-1:895812756022:table/comment-lens-development-serving"
        expected = {
            bucket: {
                "s3:ListBucket", "s3:GetBucketAcl", "s3:GetAccelerateConfiguration",
                "s3:GetAnalyticsConfiguration", "s3:GetInventoryConfiguration", "s3:GetBucketLogging",
                "s3:GetMetricsConfiguration", "s3:GetBucketNotification", "s3:GetReplicationConfiguration",
                "s3:GetBucketWebsite", "s3:GetBucketObjectLockConfiguration", "s3:ListTagsForResource",
                "s3:GetBucketAbac", "s3:GetIntelligentTieringConfiguration", "s3:GetBucketMetadataTableConfiguration",
            },
            table: {"dynamodb:DescribeContributorInsights", "dynamodb:DescribeKinesisStreamingDestination",
                    "dynamodb:GetResourcePolicy"},
            "arn:aws:cognito-idp:us-east-1:895812756022:userpool/us-east-1_*": {
                "cognito-idp:GetUserPoolMfaConfig", "cognito-idp:SetUserPoolMfaConfig",
                "cognito-idp:ListUserPoolClientSecrets",
            },
            "arn:aws:lambda:us-east-1:895812756022:function:CommentLensServing-development-*": {
                "lambda:GetFunctionCodeSigningConfig", "lambda:GetFunctionRecursionConfig",
                "lambda:GetRuntimeManagementConfig", "lambda:GetFunctionScalingConfig",
            },
            "arn:aws:logs:us-east-1:895812756022:log-group:/aws/lambda/CommentLensServing-development-*": {
                "logs:GetDataProtectionPolicy",
            },
        }
        for target, required in expected.items():
            for action in required:
                with self.subTest(action=action):
                    matching = [item for item in statements if action in actions(item)]
                    self.assertTrue(matching)
                    for item in matching:
                        resources = [item["Resource"]] if isinstance(item["Resource"], str) else item["Resource"]
                        self.assertIn(target, resources)
                        self.assertNotIn("*", resources)
                        if target == bucket:
                            self.assertEqual(resources, [bucket])
        allowed = {action for item in statements for action in actions(item)}
        # Provider schemas also cover unused optional write features. They are
        # deliberately not required for this private bucket / ordinary table.
        self.assertFalse(allowed & {"s3:PutObjectAcl", "s3:CreateBucketMetadataTableConfiguration",
                                    "dynamodb:ImportTable", "dynamodb:PutResourcePolicy", "kms:CreateGrant"})

    def test_api_stage_tagging_covers_create_collection_and_stays_regional(self):
        web = self.policies[FILES[1]]["Statement"]
        for action in ("apigateway:TagResource", "apigateway:UntagResource"):
            matching = [item for item in web if action in actions(item)]
            self.assertTrue(matching, action)
            patterns = [resource for item in matching for resource in (
                [item["Resource"]] if isinstance(item["Resource"], str) else item["Resource"])]
            self.assertEqual(set(patterns), {"arn:aws:apigateway:us-east-1::/apis",
                                            "arn:aws:apigateway:us-east-1::/apis/*"})
            # CloudFormation authorizes create-time stage tags on the collection
            # before /stages/$default exists; a stage-only pattern is insufficient.
            for target in ("arn:aws:apigateway:us-east-1::/apis",
                           "arn:aws:apigateway:us-east-1::/apis/3ppp988jf4",
                           "arn:aws:apigateway:us-east-1::/apis/3ppp988jf4/stages",
                           "arn:aws:apigateway:us-east-1::/apis/3ppp988jf4/stages/$default"):
                self.assertTrue(any(fnmatch.fnmatchcase(target, pattern) for pattern in patterns), target)
            for unrelated in ("arn:aws:apigateway:eu-west-1::/apis/example/stages",
                              "arn:aws:apigateway:us-east-1::/restapis/example/stages",
                              "arn:aws:apigateway:us-east-1::/domainnames/example.test"):
                self.assertFalse(any(fnmatch.fnmatchcase(unrelated, pattern) for pattern in patterns), unrelated)

    def test_cognito_create_tags_cover_preauthorization_without_widening_pool_management(self):
        web = self.policies[FILES[1]]["Statement"]
        target = "arn:aws:cognito-idp:us-east-1:895812756022:userpool/*"
        preauthorization = [item for item in web if item["Resource"] == target]
        self.assertEqual(len(preauthorization), 1)
        self.assertEqual(actions(preauthorization[0]), ["cognito-idp:TagResource"])
        management = next(item for item in web if "cognito-idp:DeleteUserPool" in actions(item))
        self.assertEqual(management["Resource"], "arn:aws:cognito-idp:us-east-1:895812756022:userpool/us-east-1_*")


if __name__ == "__main__":
    unittest.main()
