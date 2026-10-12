# Backend settings, entities and durable storage

The backend starts with three pieces: a settings file, Python types describing
those settings and our API data, and a manager that joins the two. We can check
these pieces before any HTTP server, queue worker, or AWS resource exists.

## Follow the data through the files

Start with [config/serving.yaml](../config/serving.yaml). This is the backend's
settings file. It contains worker limits, sample limits, retention, authentication
references and model fingerprints. The training settings remain in `config.yaml`
and `params.yaml`; serving changes do not become DVC training dependencies.

Next read [entity/serving.py](../src/Sentiment_analysis/entity/serving.py).
Each class describes one YAML section. For example, `WorkerSettings` turns
`timeout_seconds: 180` into an integer with a valid range. It also checks the
relationship between heartbeat, soft deadline, timeout and lease. The root
`ServingSettings` checks relationships across sections, such as queue visibility
being at least six worker timeouts. A quoted `"180"`, a boolean used as a count,
an unknown setting or a nonfinite probability is rejected rather than guessed.

Then read [config/serving.py](../src/Sentiment_analysis/config/serving.py).
`ServingConfigurationManager` reads YAML, rejects duplicate keys, resolves model
paths inside the project, and returns the validated settings. It does not load
model weights, deserialize joblib, restore DVC, resolve secrets or contact AWS.
It imports Pydantic and PyYAML instead of the existing training configuration
manager, which also imports scientific pipeline utilities.

```python
from src.Sentiment_analysis.config.serving import ServingConfigurationManager

settings = ServingConfigurationManager().get_serving_config()
print(settings.worker.timeout_seconds)           # 180
print(settings.youtube.working_daily_quota_units) # 8000, from the configured allowance
```

The returned models are frozen and array fields become tuples. This helps us
reuse validated settings without another part of the application changing them.
When a setting is wrong, validation identifies its section and field while
omitting the rejected value from the displayed error.

## Validate the file locally

From the repository root, install the serving dependency set:

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-serving.txt
.\.venv\Scripts\python.exe -m src.Sentiment_analysis.config.serving
```

The current file is a development configuration. Live YouTube analysis is
disabled, the budget and authentication resources are unset, and there is no
assigned serving release. The model/preprocessing checksums identify the current
local training candidate. Those checksums are declarations here; the later
worker must verify the actual bundled files against its immutable release.

Do not invent a release ID to fill an empty field. The release workflow will
assign the ID and code revision after the worker image and its golden prediction
checks exist. The API `ModelReference` entity requires a real assigned release;
the development settings allow it to remain unassigned while we build the backend.

To test whether an explicit production file declares all required settings:

```powershell
.\.venv\Scripts\python.exe -m src.Sentiment_analysis.config.serving --config config/serving.production.yaml --production
```

No production file is provided yet. Production validation requires a positive
budget, assigned release/code fingerprints, table name, matching Cognito region,
public client and extension ID, exact allowed origins, and recorded YouTube
approval/quota verification with a Secrets Manager ARN. An ARN is a reference;
the API key itself never goes into YAML. The extension client cannot have a
client secret. These checks validate declarations, not the existence of AWS
resources, IAM permissions, Google quota or the approval itself.

## API and job entities

[entity/analysis.py](../src/Sentiment_analysis/entity/analysis.py) implements the
data shapes in the [OpenAPI contract](backend-contract.yaml). Pydantic is used
here because the later FastAPI routes can use the same validated models.
There are no HTTP routes in this stage.
The later worker adapter will join shared-service predictions to YouTube
metadata to build the serving result envelope. Its cross-field checks use
[Pydantic model validators](https://pydantic.dev/docs/validation/latest/concepts/validators/).

| Entity | Its role |
| --- | --- |
| `AnalysisInput` | Validate supported HTTPS video URLs, canonicalize to one watch URL, and bound the sample size |
| `ModelReference` | Bind a response to a serving release and its model/preprocessing fingerprints |
| `Analysis`, `Progress`, `Sampling` | Describe the owner's job status and the sample actually requested/fetched |
| `Comment`, `Summary`, `AnalysisResult` | Keep predictions, skip reasons, counts and charts consistent |
| `ResultDownload` | Describe a private HTTPS result download; hide the temporary URL from `repr` |
| `ModelDetails` | Explain evaluation context and the uncalibrated class-score meaning |
| `ErrorEnvelope` | Provide stable error codes and safe retry/reset information |
| `WorkMessage` | Carry only work ID, release ID and dispatch generation to the queue |

For example, youtu.be, watch, shorts and live links to one video all produce
the same canonical request URL. That gives idempotency/cache code one input to
fingerprint. Ownership still comes from authenticated server context, not a
client-supplied owner field.

A result cannot claim ten positive comments while containing only two analyzed
rows. It checks that the label counts match the rows, skips have null
predictions, probabilities sum to one, review flags use the release threshold,
comment IDs are unique, indices preserve order, and the sample period matches
publication times. Empty samples have zero coverage and null sample dates.
Aware timestamps are serialized in UTC; naive dates and numeric epoch inputs
are rejected. Highest class score still means a confidence indicator, not a
calibrated correctness probability.

Use `model_dump(mode="json")` for a JSON-compatible dictionary or
`model_dump_json()` for JSON text. Required nullable fields stay present. Optional
error retry fields are omitted when absent, matching the contract. Shared result
entities reject unknown fields, including user ownership data.

## Checking settings and contracts

The serving tests use synthetic metadata, temporary YAML files and mocked AWS
services. They require no dataset, model, real credentials or AWS account:

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-serving-test.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests/serving -t . -v
```

The tests cover configuration relationships, incomplete production settings,
secret/region/origin/path guards, YAML ambiguity, canonical URLs, timestamp
handling, invalid predictions and schema compatibility. The full repository
suite also runs them through the existing CI workflow.

## Durable jobs and private storage

An HTTP request lasts a few seconds. Fetching and analyzing comments takes
longer, and the browser may close while it runs. That is why the next step puts
the job in durable storage before returning an analysis ID. If an API or worker
process stops, another process can read the same record and continue.

Two people can analyze the same public video. We keep one internal `work_id`
and give each person a different owned `analysis_id`. They share the computation,
while status, download and deletion checks use their own request record. The
later API must supply the authenticated Cognito subject; an owner supplied in a
request body will never be used.

Read these files in this order:

1. [entity/jobs.py](../src/Sentiment_analysis/entity/jobs.py) describes the
   persisted work, owned request and S3 pointer. These internal entities are
   separate from the public API responses.
2. [repositories/dynamo.py](../src/Sentiment_analysis/repositories/dynamo.py)
   provides strongly consistent reads and conditional transactions. Each item
   has a version. Updating the wrong version makes the whole transaction fail.
3. [repositories/jobs.py](../src/Sentiment_analysis/repositories/jobs.py)
   uses those transactions for admission, ownership, leases and quota accounting.
4. [integrations/objects.py](../src/Sentiment_analysis/integrations/objects.py)
   stores immutable JSON objects, checks their size/hash, and signs authorized
   downloads.
5. [components/job_delivery.py](../src/Sentiment_analysis/components/job_delivery.py)
   sends outbox messages; [pipeline/job_maintenance.py](../src/Sentiment_analysis/pipeline/job_maintenance.py)
   coordinates recovery and cleanup.
6. [workers/dispatch.py](../src/Sentiment_analysis/workers/dispatch.py) and
   [workers/reconcile.py](../src/Sentiment_analysis/workers/reconcile.py) are the
   Lambda entry points. Importing them does not create clients or load a model.

### What a submission commits

For a new video sample, `admit()` commits the work record, owned request,
idempotency record, cache claim, quota reservation, global/user limits, object
cleanup marker and outbox in one transaction. Either all of them exist or none
of them does. A competing cache claim causes a re-read so the losing request
can join the winning job. Running and completed cache joins create their own
request/idempotency/limit records and check the existing work version without
rewriting the shared work.

The idempotency key is a UUID scoped to the owner. Retrying the same canonical
body returns the original analysis ID. Using the same key for another body
raises `IDEMPOTENCY_CONFLICT`. Even if the first commit succeeds and its network
response is lost, retrying does not reserve quota or create a second job.
This follows DynamoDB's [conditional transaction behavior](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html).

Read/submission/provider limits use fixed minute or second counters. Boundary
bursts can straddle two windows; these are fixed-window caps, not a promise of
an exact rolling-window rate. `charge_read()` is ready for the authenticated API
to call. There are no HTTP endpoints or Cognito integration in this stage.

### Why there is an outbox

Saving a job and sending an SQS message are two separate operations. Sending
first risks a worker seeing a missing job. Saving first risks losing the message
if the process stops. The outbox solves this by saving the delivery intent in
the admission transaction.

The dispatcher reads that intent, sends the small `WorkMessage`, then marks the
outbox sent. A crash after sending leaves it pending, so recovery may send it
again. The worker claim is conditional, making this duplicate harmless. An
expired lease creates a new dispatch generation; old generations cannot claim
the recovered job. Obsolete outbox records leave the due index so they cannot
block later deliveries.

A claim transaction conflict is retried. If it keeps conflicting, `claim()`
raises `SYSTEM_BUSY`; the later SQS worker must report a batch failure for it.
Only a verified duplicate, obsolete generation, terminal or expired job returns
no claim. A conflict alone must not acknowledge a still-queued job.

### Why workers have fencing tokens

Claiming queued work increments a lease token. Heartbeats, checkpoint pointers,
quota spending and terminal publication must match the current token and an
unexpired lease/deadline. If worker A pauses and worker B recovers the job, A's
token cannot replace B's state or publish its old result.

Each attempt writes under:

```text
serving/<environment>/work/<work-id>/attempt/<lease-token>/
  checkpoints/<sha256>.json
  results/<sha256>.json
```

S3 writes use `IfNoneMatch="*"`, JSON content type and AES256 encryption. The
hash is computed over the exact serialized bytes, and the 10 MiB ceiling is
checked before upload. Retrying identical bytes verifies the existing object.
AWS documents [conditional S3 writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html).

Uploading is only the first half of publication. A terminal transaction publishes
the pointer and releases the global active slot and unused quota exactly once.
The preceding `SAVING` state and counters must reconcile. A lost lease leaves an
unpublished attempt object for cleanup; it does not make the result available.

Quota units move from reserved to spent **before** each provider attempt. Failed
calls are never refunded. Retry invocations reuse the remaining allowance.
Pacific midnight moves that remaining reservation to the new day's counter in
the same transaction as the next debit, or refuses the attempt. The code handles
daylight-saving changes. This accounting is fixture-tested; the actual Google
project allocation and permission to run live analysis remain unverified.

### Ownership, expiration and cleanup

Unknown and foreign request IDs produce the same `NOT_FOUND` error.
An expired/deleted owned reference produces `EXPIRED`. Deletion removes
that owner's active slot and access; it does not cancel work shared with others.
Download signing reads the owned request again and accepts only its published
result pointer. Links last at most 300 seconds, stop at request/result expiry,
and can be shortened to match temporary credential expiry. A previously issued
link remains usable until its own expiry.

At result expiry, an `OBJECTS#work-id` cleanup marker causes the schedule to
remove all objects for that work, including orphaned attempts, noncurrent
versions and delete markers. This matters when the existing dataset bucket uses
versioning: [deleting only a current key can retain older versions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/DeletingObjects.html).
Cleanup touches only the specific serving work prefix. It never deletes the
DVC dataset prefix.

Expired request/work metadata stays for one additional hour so the API can
distinguish expiration from an unknown ID, then gets purged. If S3 deletion
fails, the cleanup marker and work metadata stay for retry. Empty quota audit
records are cleaned eight Pacific midnights after their date; counters with
outstanding reservations are kept. The permanent global active counter has no
expiry. Every freshness/lease/retention decision checks timestamps directly;
DynamoDB TTL deletion is not part of correctness.

### Table and runtime wiring for deployment

The future CDK table must use string partition key `pk` and these indexes, all
with `ALL` projection:

| Index | Partition key | Sort key | Purpose |
| --- | --- | --- | --- |
| `DueIndex` | `due_pk` (string) | `due_at` (number) | Pending outbox and work lease/deadline recovery |
| `CleanupIndex` | `cleanup_pk` (string) | `cleanup_at` (number) | Expired objects and metadata |
| `OwnerIndex` | `owner_pk` (string) | `created_at` (number) | Paginated owned request lookup |

Due/cleanup queries process up to 100 items per family per invocation. More due
items remain for the next minute; recovery lag and cleanup backlog need alarms
and load measurement before release. Index reads may lag, so actions re-read
the base item and check its version. Polling uses direct request/work reads.

The table stream calls `workers.dispatch.handler` with partial batch failure
reporting enabled. A minute schedule calls `workers.reconcile.handler`. The
schedule returns safe aggregate counts and reports an invocation failure if any
operation needs retry. IAM roles, triggers and schedules have not been created.

[config/serving_runtime.py](../src/Sentiment_analysis/config/serving_runtime.py)
loads the serving YAML lazily and uses AWS role credentials. Deployment must
assign `storage.table_name`. `SERVING_CONFIG_PATH` selects its config file, and
`SERVING_QUEUE_REGISTRY_JSON` maps immutable release IDs to distinct regional
standard SQS queue URLs. Missing/wrong-region/shared/FIFO queues are rejected.
The runtime sets bounded SDK timeouts/retries and uses SigV4 for S3 downloads.
It never reads the local DVC credentials file.

The later IAM setup must grant prefix-scoped S3 writes/reads plus
`s3:ListBucketVersions`, `s3:DeleteObject` and `s3:DeleteObjectVersion` to the
cleanup role. Keep the bucket private with public-access blocking and a
seven-day lifecycle backstop covering current/noncurrent objects. The existing
DVC IAM policy is separate and does not provide these serving permissions.

### Reproduce the storage checks

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-serving-test.txt
.\.venv\Scripts\python.exe -m unittest tests.serving.test_jobs -v
```

The tests inject fake AWS credentials, use Moto's in-process DynamoDB/S3/SQS
implementations, and control the clock. They cover transaction conflicts,
response loss, shared ownership, idempotency, cache expiry, burst caps, stale
leases/results, dispatch crashes, quota rollover, versioned cleanup and signing.
These checks verify the adapters and failure protocol without a live AWS account.
They are not an AWS capacity measurement; the planned load/deployment checks
still need to run against the eventual environment.

The next stage is the FastAPI pipeline: create/status/download/delete/current
model/liveness routes using these repositories. Model inference and live
YouTube fetching remain later steps in the [backend plan](backend-plan.md).
