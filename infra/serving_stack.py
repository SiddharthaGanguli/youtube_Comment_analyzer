"""Serverless serving resources, synthesized without AWS lookups or Docker.

The deployment script uploads separately built Linux ZIPs first. This stack
never packages local credentials, datasets or training environments.
"""

import json

from aws_cdk import Aws, BootstraplessSynthesizer, CfnOutput, CfnParameter, CfnRule, CfnRuleAssertion, Fn, RemovalPolicy
from aws_cdk import aws_cloudwatch as cloudwatch, aws_dynamodb as dynamodb
from aws_cdk import aws_events as events, aws_iam as iam, aws_lambda as lambda_
from aws_cdk import aws_logs as logs, aws_s3 as s3, aws_sqs as sqs

from infra.auth_stack import AuthStack


WEB_CALLBACK_URL = "https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html"
DEVELOPMENT_CALLBACK_URLS = (
    "http://127.0.0.1:8766/dashboard.html",
    "http://localhost:8766/dashboard.html",
)


class ServingStack(AuthStack):
    """Own the table, private result bucket, delivery pipeline and JWT API.

    Shared development mode uses the account's unreserved Lambda pool. In that
    explicit prototype mode, the existing runtime ``reserved_concurrency``
    fields describe requested sizing budgets, not allocated AWS reservations.
    It provides no reserved capacity or isolation between the functions.
    """

    def __init__(self, scope, id, *, auth, settings, live_youtube=False,
                 web_callback_url=WEB_CALLBACK_URL, extension_id=None,
                 shared_development_concurrency=False, **kwargs):
        if auth.environment != settings.environment or auth.region != settings.region:
            raise ValueError("Serving and authorization environments/regions must match")
        if shared_development_concurrency and settings.environment != "development":
            raise ValueError("Shared Lambda concurrency is available only for explicit development prototypes")
        if live_youtube and not (settings.youtube.api_key_secret_arn and settings.youtube.project_quota_verified):
            raise ValueError("Live deployment requires a secret reference and verified quota")
        # The deploy script supplies S3 code/template locations directly. There
        # are no CDK assets or bootstrap resources to look up or provision.
        kwargs.setdefault("synthesizer", BootstraplessSynthesizer())
        super().__init__(scope, id, auth=auth, api_limits=settings.api,
                         observability=settings.observability, defer_authorization=True, **kwargs)
        if len(self.stack_name) > 44:
            raise ValueError("Use a stack name of at most 44 characters for Lambda and role names")
        concurrency_mode = "shared-development" if shared_development_concurrency else "reserved"
        metadata = {"CommentLensReleaseWorkflow": "first-development-release"
            if settings.environment == "development" else "production-release-requires-reviewed-rollout",
            "CommentLensConcurrencyMode": concurrency_mode}
        if shared_development_concurrency:
            metadata["CommentLensConcurrencyNote"] = (
                "Development prototype: runtime reserved_concurrency numbers are requested sizing budgets, "
                "not allocated Lambda reservations. All functions share the account pool without capacity isolation.")
        self.template_options.metadata = metadata
        self.settings = settings
        artifacts = CfnParameter(self, "RuntimeArtifactsBucket", type="String",
            allowed_pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
            description="Private same-region bucket holding the verified deployment ZIPs")
        api_key = CfnParameter(self, "ApiCodeKey", type="String", min_length=1, max_length=1024,
            allowed_pattern=r"^[A-Za-z0-9_./-]+\.zip$", description="Immutable SHA256-addressed API ZIP key")
        worker_key = CfnParameter(self, "WorkerCodeKey", type="String", min_length=1, max_length=1024,
            allowed_pattern=r"^[A-Za-z0-9_./-]+\.zip$", description="Immutable SHA256-addressed worker ZIP key")
        revision = CfnParameter(self, "CodeRevision", type="String", allowed_pattern=r"^[a-f0-9]{40}$")
        release = CfnParameter(self, "ModelReleaseId", type="String", allowed_pattern=r"^sr_[a-f0-9]{64}$")
        approval = CfnParameter(self, "AnalyticsUseApprovalReference", type="String", default="", max_length=1024,
            allowed_pattern=r'^[^"\\\x00-\x1F\x7F]*$',
            description="Accepted YouTube analytics-use approval reference; submission alone is not approval")
        if live_youtube:
            CfnRule(self, "RequireAcceptedApproval", assertions=[CfnRuleAssertion(
                assert_=Fn.condition_not(Fn.condition_equals(approval.value_as_string, "")),
                assert_description="Live fetching requires the accepted analytics-use approval reference")])

        table = dynamodb.CfnTable(self, "ServingTable", billing_mode="PAY_PER_REQUEST",
            table_name=f"comment-lens-{settings.environment}-serving",
            deletion_protection_enabled=True,
            attribute_definitions=[dynamodb.CfnTable.AttributeDefinitionProperty(attribute_name=name, attribute_type=kind)
                for name, kind in (("pk", "S"), ("due_pk", "S"), ("due_at", "N"), ("cleanup_pk", "S"),
                                   ("cleanup_at", "N"), ("owner_pk", "S"), ("created_at", "N"))],
            key_schema=[dynamodb.CfnTable.KeySchemaProperty(attribute_name="pk", key_type="HASH")],
            global_secondary_indexes=[dynamodb.CfnTable.GlobalSecondaryIndexProperty(index_name=name,
                key_schema=[dynamodb.CfnTable.KeySchemaProperty(attribute_name=partition, key_type="HASH"),
                            dynamodb.CfnTable.KeySchemaProperty(attribute_name=sort, key_type="RANGE")],
                projection=dynamodb.CfnTable.ProjectionProperty(projection_type="ALL"))
                for name, partition, sort in (("DueIndex", "due_pk", "due_at"),
                    ("CleanupIndex", "cleanup_pk", "cleanup_at"), ("OwnerIndex", "owner_pk", "created_at"))],
            stream_specification=dynamodb.CfnTable.StreamSpecificationProperty(stream_view_type="NEW_IMAGE"),
            point_in_time_recovery_specification=dynamodb.CfnTable.PointInTimeRecoverySpecificationProperty(
                point_in_time_recovery_enabled=settings.environment == "production"))
        table.apply_removal_policy(RemovalPolicy.RETAIN)
        prefix = settings.storage.serving_prefix
        bucket = s3.CfnBucket(self, "ResultsBucketResource",
            bucket_name=Fn.join("-", ["comment-lens", settings.environment, Aws.ACCOUNT_ID, Aws.REGION, "results"]),
            public_access_block_configuration=s3.CfnBucket.PublicAccessBlockConfigurationProperty(
                block_public_acls=True, block_public_policy=True, ignore_public_acls=True, restrict_public_buckets=True),
            ownership_controls=s3.CfnBucket.OwnershipControlsProperty(rules=[
                s3.CfnBucket.OwnershipControlsRuleProperty(object_ownership="BucketOwnerEnforced")]),
            bucket_encryption=s3.CfnBucket.BucketEncryptionProperty(server_side_encryption_configuration=[
                s3.CfnBucket.ServerSideEncryptionRuleProperty(server_side_encryption_by_default=
                    s3.CfnBucket.ServerSideEncryptionByDefaultProperty(sse_algorithm="AES256"))]),
            lifecycle_configuration=s3.CfnBucket.LifecycleConfigurationProperty(rules=[
                s3.CfnBucket.RuleProperty(id="ServingDataBackstop", prefix=prefix + "/", status="Enabled",
                    expiration_in_days=settings.cache.s3_cleanup_backstop_days,
                    noncurrent_version_expiration=s3.CfnBucket.NoncurrentVersionExpirationProperty(
                        noncurrent_days=settings.cache.s3_cleanup_backstop_days),
                    abort_incomplete_multipart_upload=s3.CfnBucket.AbortIncompleteMultipartUploadProperty(days_after_initiation=1))]))
        bucket.apply_removal_policy(RemovalPolicy.RETAIN)
        bucket_arn = bucket.attr_arn
        object_arn = Fn.join("", [bucket_arn, "/", prefix, "/work/*"])
        s3.CfnBucketPolicy(self, "ResultsTransportPolicy", bucket=bucket.ref, policy_document={
            "Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Principal": "*", "Action": "s3:*",
                "Resource": [bucket_arn, Fn.join("", [bucket_arn, "/*"])],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]})
        work_dlq = sqs.CfnQueue(self, "WorkerDeadLetterQueue", queue_name=self.stack_name + "-worker-dlq", message_retention_period=1209600,
                               sqs_managed_sse_enabled=True)
        delivery_dlq = sqs.CfnQueue(self, "DeliveryDeadLetterQueue", queue_name=self.stack_name + "-delivery-dlq", message_retention_period=1209600,
                                   sqs_managed_sse_enabled=True)
        queue = sqs.CfnQueue(self, "ReleaseQueue", visibility_timeout=settings.queue.visibility_timeout_seconds,
            queue_name=Fn.join("", ["cl-dev-" if settings.environment == "development" else "cl-prod-", release.value_as_string]),
            message_retention_period=86400, sqs_managed_sse_enabled=True,
            redrive_policy={"deadLetterTargetArn": work_dlq.attr_arn, "maxReceiveCount": settings.queue.maximum_receive_count})
        queue.apply_removal_policy(RemovalPolicy.RETAIN)

        api_concurrency = min(settings.api.reserved_concurrency, 10) if settings.environment == "development" else settings.api.reserved_concurrency
        worker_concurrency = 2 if shared_development_concurrency else settings.worker.reserved_concurrency
        mapping_concurrency = 2 if shared_development_concurrency else settings.worker.sum_of_release_mapping_maximum_concurrency
        api, api_role = self._function("Api", "api", "src.Sentiment_analysis.api.lambda_handler.handler",
            artifacts.value_as_string, api_key.value_as_string, settings.api.memory_mib,
            settings.api.timeout_seconds, None if shared_development_concurrency else api_concurrency)
        worker, worker_role = self._function("Worker", "worker", "src.Sentiment_analysis.workers.analyze.handler",
            artifacts.value_as_string, worker_key.value_as_string, settings.worker.memory_mib,
            settings.worker.timeout_seconds, None if shared_development_concurrency else worker_concurrency)
        dispatcher, dispatcher_role = self._function("Dispatcher", "dispatcher", "src.Sentiment_analysis.workers.dispatch.handler",
            artifacts.value_as_string, api_key.value_as_string, 256, 30, None if shared_development_concurrency else 2)
        reconciler, reconciler_role = self._function("Reconciler", "reconciler", "src.Sentiment_analysis.workers.reconcile.handler",
            artifacts.value_as_string, api_key.value_as_string, 256, 60, None if shared_development_concurrency else 1)
        verifier, _ = self._function("Verifier", "verify-release", "src.Sentiment_analysis.workers.verify_release.handler",
            artifacts.value_as_string, worker_key.value_as_string, settings.worker.memory_mib, 60, None if shared_development_concurrency else 1)

        callbacks = [web_callback_url]
        if settings.environment == "development":
            callbacks.extend(DEVELOPMENT_CALLBACK_URLS)
        self.configure_authorization(auth=auth, api_limits=settings.api, observability=settings.observability,
            function_arn=api.attr_arn, include_extension=extension_id is not None,
            extension_id=extension_id, web_callback_urls=tuple(callbacks))
        connection = self.authorization
        bucket.cors_configuration = s3.CfnBucket.CorsConfigurationProperty(cors_rules=[
            s3.CfnBucket.CorsRuleProperty(allowed_methods=["GET"], allowed_origins=connection["origins"],
                exposed_headers=["ETag", "Content-Length", "Content-Type", "x-amz-checksum-sha256"], max_age=600)])

        overrides = {"model": {"release_id": release.value_as_string, "code_revision": revision.value_as_string},
            "storage": {"bucket": bucket.ref, "serving_prefix": prefix, "table_name": table.ref},
            "auth": {"cognito_issuer": connection["issuer"], "public_client_id": connection["client"].ref,
                     "extension_id": extension_id},
            "api": {"allowed_origins": connection["origins"], "reserved_concurrency": api_concurrency},
            "worker": {"reserved_concurrency": worker_concurrency,
                       "sum_of_release_mapping_maximum_concurrency": mapping_concurrency},
            "youtube": {"live_enabled": live_youtube, "analytics_use_approved": live_youtube,
                        "approval_reference": approval.value_as_string if live_youtube else None}}
        runtime = {"SERVING_CONFIG_PATH": "config/serving.yaml", "SERVING_RUNTIME_OVERRIDES_JSON": self.to_json_string(overrides)}
        for function in (api, worker, dispatcher, reconciler, verifier):
            function.environment = lambda_.CfnFunction.EnvironmentProperty(variables=dict(runtime))
        for function in (worker, verifier):
            function.environment = lambda_.CfnFunction.EnvironmentProperty(variables={**runtime,
                "SERVING_IMAGE_CODE_REVISION": revision.value_as_string,
                "OMP_NUM_THREADS": str(settings.worker.native_thread_limit),
                "OPENBLAS_NUM_THREADS": str(settings.worker.native_thread_limit),
                "MKL_NUM_THREADS": str(settings.worker.native_thread_limit)})
        registry = self.to_json_string({release.value_as_string: queue.ref})
        for function in (dispatcher, reconciler):
            function.environment = lambda_.CfnFunction.EnvironmentProperty(variables={**runtime,
                "SERVING_QUEUE_REGISTRY_JSON": registry})

        for role in (api_role, worker_role, dispatcher_role, reconciler_role):
            self._policy(role, "TableReads", ["dynamodb:GetItem"], [table.attr_arn])
            # TransactWriteItems uses the permissions of its underlying actions.
            # https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html
            self._policy(role, "TableTransactions", ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"],
                [table.attr_arn], {"ForAnyValue:StringEquals": {"dynamodb:EnclosingOperation": ["TransactWriteItems"]}})
        self._policy(api_role, "OwnedIndex", ["dynamodb:Query"], [table.attr_arn, Fn.join("", [table.attr_arn, "/index/OwnerIndex"])])
        self._policy(reconciler_role, "MaintenanceIndexes", ["dynamodb:Query"], [table.attr_arn, *[
            Fn.join("", [table.attr_arn, "/index/", index]) for index in ("DueIndex", "CleanupIndex")]])
        self._policy(api_role, "SignedResults", ["s3:GetObject"], [Fn.join("", [bucket_arn, "/", prefix,
            "/work/*/attempt/*/results/*.json"])])
        self._policy(worker_role, "WorkerObjects", ["s3:GetObject", "s3:PutObject"], [object_arn])
        if settings.youtube.api_key_secret_arn:
            self._policy(worker_role, "YouTubeSecret", ["secretsmanager:GetSecretValue"], [settings.youtube.api_key_secret_arn])
        self._policy(reconciler_role, "DeleteExpiredObjects", ["s3:DeleteObject", "s3:DeleteObjectVersion"], [object_arn])
        self._policy(reconciler_role, "ListExpiredVersions", ["s3:ListBucketVersions"], [bucket_arn],
                     {"StringLike": {"s3:prefix": [prefix + "/work/*"]}})
        for role in (dispatcher_role, reconciler_role):
            self._policy(role, "SendReleaseMessages", ["sqs:SendMessage"], [queue.attr_arn])
        self._policy(worker_role, "ConsumeReleaseMessages", ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], [queue.attr_arn])
        self._policy(dispatcher_role, "ReadOutboxStream", ["dynamodb:DescribeStream", "dynamodb:GetRecords",
            "dynamodb:GetShardIterator"], [table.attr_stream_arn])
        # ListStreams does not support resource-level IAM permissions. Its
        # regional metadata permission cannot authorize reading another stream.
        self._policy(dispatcher_role, "DiscoverOutboxStream", ["dynamodb:ListStreams"], ["*"],
                     {"StringEquals": {"aws:RequestedRegion": settings.region}})
        self._policy(dispatcher_role, "FailedStreamRecords", ["sqs:SendMessage"], [delivery_dlq.attr_arn])

        lambda_.CfnEventSourceMapping(self, "OutboxStreamMapping", function_name=dispatcher.ref,
            event_source_arn=table.attr_stream_arn, starting_position="TRIM_HORIZON", batch_size=100,
            maximum_batching_window_in_seconds=0, bisect_batch_on_function_error=True,
            maximum_record_age_in_seconds=3600, maximum_retry_attempts=3,
            function_response_types=["ReportBatchItemFailures"],
            destination_config=lambda_.CfnEventSourceMapping.DestinationConfigProperty(on_failure=
                lambda_.CfnEventSourceMapping.OnFailureProperty(destination=delivery_dlq.attr_arn)),
            filter_criteria=lambda_.CfnEventSourceMapping.FilterCriteriaProperty(filters=[
                lambda_.CfnEventSourceMapping.FilterProperty(pattern=json.dumps({"eventName": ["INSERT", "MODIFY"],
                    "dynamodb": {"Keys": {"pk": {"S": [{"prefix": "OUTBOX#"}]}}}}, separators=(",", ":")))]))
        lambda_.CfnEventSourceMapping(self, "ReleaseQueueMapping", function_name=worker.ref,
            event_source_arn=queue.attr_arn, batch_size=settings.queue.event_batch_size,
            maximum_batching_window_in_seconds=settings.queue.event_batch_window_seconds,
            function_response_types=["ReportBatchItemFailures"], enabled=live_youtube,
            scaling_config=lambda_.CfnEventSourceMapping.ScalingConfigProperty(
                maximum_concurrency=mapping_concurrency))
        rule = events.CfnRule(self, "MaintenanceSchedule", schedule_expression=f"rate({settings.queue.reconciler_interval_seconds // 60} minute{'' if settings.queue.reconciler_interval_seconds == 60 else 's'})",
            state="ENABLED", targets=[events.CfnRule.TargetProperty(arn=reconciler.attr_arn, id="Reconcile",
                retry_policy=events.CfnRule.RetryPolicyProperty(maximum_retry_attempts=2, maximum_event_age_in_seconds=120))])
        lambda_.CfnPermission(self, "AllowScheduledMaintenance", action="lambda:InvokeFunction",
            function_name=reconciler.ref, principal="events.amazonaws.com", source_account=Aws.ACCOUNT_ID, source_arn=rule.attr_arn)
        for mapping in ("OutboxStreamMapping", "ReleaseQueueMapping"):
            # Permissions must exist before Lambda validates either event source.
            resource = self.node.find_child(mapping)
            for policy in self.node.find_all():
                if isinstance(policy, iam.CfnPolicy):
                    resource.add_resource_dependency(policy)

        self._alarm("QueueAge", "AWS/SQS", "ApproximateAgeOfOldestMessage", "Maximum",
            settings.observability.queue_age_alarm_seconds, [{"name": "QueueName", "value": queue.attr_queue_name}])
        for name, dead in (("WorkerDlqMessages", work_dlq), ("DeliveryDlqMessages", delivery_dlq)):
            self._alarm(name, "AWS/SQS", "ApproximateNumberOfMessagesVisible", "Maximum", 1,
                [{"name": "QueueName", "value": dead.attr_queue_name}])
        for name, function in (("ApiLambdaErrors", api), ("WorkerLambdaErrors", worker),
                               ("DispatchLambdaErrors", dispatcher), ("MaintenanceLambdaErrors", reconciler)):
            self._alarm(name, "AWS/Lambda", "Errors", "Sum", 1, [{"name": "FunctionName", "value": function.ref}])
        # S3's us-east-1 default endpoint is used by the existing signing adapter.
        result_base = Fn.join("", ["https://", bucket.ref, ".s3.amazonaws.com/", prefix])
        for name, value in {"ResultsBucket": bucket.ref, "ResultBaseUrl": result_base, "TableName": table.ref,
                "QueueUrl": queue.ref, "ApiFunctionName": api.ref, "WorkerFunctionName": worker.ref,
                "VerifierFunctionName": verifier.ref,
                "ServingReleaseId": release.value_as_string, "LiveYouTubeEnabled": str(live_youtube).lower(),
                "AcceptedApprovalReference": approval.value_as_string, "ConcurrencyMode": concurrency_mode,
                "ApiConcurrencyBudget": str(api_concurrency), "WorkerConcurrencyBudget": str(worker_concurrency),
                "WorkerMaximumConcurrency": str(mapping_concurrency)}.items():
            CfnOutput(self, name, value=value)
        if settings.environment == "development":
            CfnOutput(self, "LocalCallbackUrl", value=DEVELOPMENT_CALLBACK_URLS[0])

    def _function(self, logical, suffix, handler, bucket, key, memory, timeout, concurrency):
        name = self.stack_name + "-" + suffix
        group = logs.CfnLogGroup(self, logical + "Log", log_group_name="/aws/lambda/" + name,
                                 retention_in_days=self.settings.observability.log_retention_days)
        role = iam.CfnRole(self, logical + "Role", assume_role_policy_document={"Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "lambda.amazonaws.com"}}]},
            role_name=name + "-role", permissions_boundary=Fn.join("", ["arn:", Aws.PARTITION, ":iam::", Aws.ACCOUNT_ID,
                ":policy/CommentLensServing", self.settings.environment.title(), "RuntimeBoundary"]))
        self._policy(role, "FunctionLogs", ["logs:CreateLogStream", "logs:PutLogEvents"], [group.attr_arn])
        function = lambda_.CfnFunction(self, logical + "Function", function_name=name, role=role.attr_arn,
            code=lambda_.CfnFunction.CodeProperty(s3_bucket=bucket, s3_key=key), handler=handler, runtime="python3.12",
            architectures=["x86_64"], memory_size=memory, timeout=timeout, reserved_concurrent_executions=concurrency,
            logging_config=lambda_.CfnFunction.LoggingConfigProperty(log_format="Text", log_group=group.ref))
        function.add_resource_dependency(group)
        return function, role

    def _policy(self, role, name, actions, resources, conditions=None):
        statement = {"Effect": "Allow", "Action": actions, "Resource": resources}
        if conditions:
            statement["Condition"] = conditions
        return iam.CfnPolicy(self, role.node.id + name, policy_name=name, roles=[role.ref],
            policy_document={"Version": "2012-10-17", "Statement": [statement]})

    def _alarm(self, name, namespace, metric, statistic, threshold, dimensions):
        return cloudwatch.CfnAlarm(self, name, namespace=namespace, metric_name=metric, statistic=statistic,
            dimensions=[cloudwatch.CfnAlarm.DimensionProperty(**dimension) for dimension in dimensions],
            period=300, evaluation_periods=1, threshold=threshold,
            comparison_operator="GreaterThanOrEqualToThreshold", treat_missing_data="notBreaching")
