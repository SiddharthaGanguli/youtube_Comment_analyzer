# Backend and extension serving walkthrough

The backend starts with three pieces: a settings file, Python types describing
those settings and our API data, and a manager that joins the two. We can check
these pieces before any HTTP server, queue worker, or AWS resource exists.

## Follow the data through the files

Start with [config/serving.yaml](../config/serving.yaml). This is the backend's
settings file. It contains worker limits, sample limits, retention, authentication
references and model fingerprints. The training settings remain in `config.yaml`
and `params.yaml`; serving YAML changes do not invalidate DVC training. The
shared prediction source is an existing training dependency, so its runtime
thread override requires refreshing that dependency's lock metadata. The saved
training recipe and weights stay unchanged; this does not represent retraining.

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
HTTP routes now use these entities in the API stage described below.
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
to call. The storage stage added no HTTP endpoints; the API stage below adds
routes and consumes verified Cognito context. AWS auth deployment remains pending.

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

## API routes and local running

The API accepts work and returns progress; the queue worker does the longer
fetching and inference. Keeping that boundary lets the HTTP process answer
other people while an analysis is running. The API package imports no NumPy,
pandas, scikit-learn or MLflow, and it never restores DVC or reads model weights.

Read [entity/api.py](../src/Sentiment_analysis/entity/api.py) first. `Principal`
represents an authenticated owner and scopes. `Liveness` is the small public
health response. `ReleaseManifest` binds model/preprocessing checksums, the
inference code revision, review threshold and schema to a deterministic release
ID. These metadata declarations still require worker-bundle verification before
deployment; no real release is assigned in the current development YAML.

Then read [repositories/models.py](../src/Sentiment_analysis/repositories/models.py).
It keeps immutable `MODEL#release-id` manifests and a conditional `MODEL#CURRENT`
pointer. Registration refuses to change an existing manifest. Promotion requires
the expected current release so an operator cannot silently overwrite another
promotion. The later release workflow must verify the image/queue before calling
this internal method. There is no public promotion endpoint.

[pipeline/api.py](../src/Sentiment_analysis/pipeline/api.py) contains the use cases.
It joins the job repository, private object store and release registry. The
current release must match the API's serving settings before a submission is
accepted. A status/download read also expires a job whose overall deadline has
passed, even if the scheduled reconciler has not run yet.

[api/routes.py](../src/Sentiment_analysis/api/routes.py) then connects those use
cases to HTTP. The routes are deliberately short: authenticate, validate typed
input, call the pipeline and return its typed result. Boto3 operations run in
FastAPI's synchronous route thread pool, rather than blocking its async request
loop. No route fetches comments, sends SQS messages or predicts sentiment.

| Route | Required scope | Normal response |
| --- | --- | --- |
| `POST /v1/analyses` | `analysis/write` | `202` for queued/running work; `200` for completed cache/replay |
| `GET /v1/analyses/{id}` | `analysis/read` | `200` owned status, including failed jobs |
| `GET /v1/analyses/{id}/result` | `analysis/read` | `200` signed S3 download descriptor |
| `DELETE /v1/analyses/{id}` | `analysis/write` | `204`, also for a repeated owned deletion |
| `GET /v1/models/current` | `analysis/read` | `200` release/evaluation metadata |
| `GET /health/live` | Public | `200` with `{"status":"ok"}` |

An accepted response includes the relative owned status path in `Location` and
the first polling delay in `Retry-After`. Download descriptors use `no-store`;
the API does not load or proxy the comment JSON. Failed jobs have a readable
status but their result endpoint returns `409 ANALYSIS_FAILED`. Pending results
return `409 RESULT_NOT_READY`. Expired/removed owned references return `410`, then
`404` after purge. Foreign and unknown IDs both return `404`.

### Where authentication comes from

[api/auth.py](../src/Sentiment_analysis/api/auth.py) consumes the verified JWT
claims/scopes that API Gateway places in Mangum's `aws.event` connection scope.
It checks the Cognito issuer, public client, access-token use, expiry, owner UUID
and the route's scope. It never treats an `X-User-Id` header as identity or decodes
an unsigned bearer string into a trusted user.

API Gateway is the signature-verification boundary. Deployment must use its
[JWT authorizer](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html),
set the required scope on each protected route, and restrict Lambda invocation
permissions to the intended HTTP API. A Lambda function URL or arbitrary direct
invoker must not become an alternate authenticated entry point. The app's scope
check adds a second check; it does not replace gateway signature verification.
Mangum documents how it exposes the [event/context in ASGI scope](https://mangum.fastapiexpert.com/adapter/).

The test gateway stand-in lives only in `tests/serving/test_api.py`. It maps
synthetic tokens to synthetic verified contexts. It is not a runtime auth mode.
Similarly, `APIPipeline(fixture_mode=True)` permits fixture admission only in
development. The deployed `api_runtime()` does not enable it. Live submissions
remain `503 LIVE_ANALYSIS_DISABLED` until the serving settings allow them;
existing status/download/delete operations do not require live fetching.

### Request and error handling

[api/middleware.py](../src/Sentiment_analysis/api/middleware.py) checks the POST
body before JSON/Pydantic validation. It counts actual bytes, including streamed
chunks, so a missing or false `Content-Length` cannot bypass the 2,048-byte cap.
Duplicate JSON fields/retry headers, unsupported content encoding, malformed
JSON and nonfinite constants are rejected. Rejected values are never echoed.

[api/errors.py](../src/Sentiment_analysis/api/errors.py) maps domain errors to
the contract's safe envelope. It includes a server-generated request ID, retry
information when useful, and the next Pacific midnight for daily quota failures.
Validation errors return `422`, body overflow returns `413`, user limits return
`429`, and busy/disabled/dependency/quota conditions return `503`. Unexpected
dependency errors produce a generic response and a generic warning; exception
text, tokens and signed URLs stay out of the response and application logs.
API Gateway can still generate its own authentication/throttle error body.

Authenticated GETs and DELETEs share the owner's read budget. POST admission
uses the existing submission/active/global counters. A denied authentication,
missing scope or invalid body does not create a job or reserve provider quota.
CORS permits only the exact configured extension origins and exposes polling
headers; bearer authentication is still required for actual protected requests.

### Start the local app

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-serving.txt
.\.venv\Scripts\python.exe -m uvicorn src.Sentiment_analysis.api.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open `http://127.0.0.1:8000/health/live` for the process check and
`http://127.0.0.1:8000/docs` for development Swagger. Creating the app and reading
these routes does not initialize AWS clients. Liveness confirms that HTTP can
respond; it does not claim storage, quota or workers are ready.

Local Swagger bearer entry cannot create a gateway-verified context, so protected
requests return `401` on the default standalone server. Use the offline fixture
tests below to exercise the complete HTTP flow until Cognito/gateway deployment
exists. Development exposes `/docs` and `/openapi.json`; production keeps those
public documentation routes disabled.

[api/app.py](../src/Sentiment_analysis/api/app.py) creates the app and its lazy
pipeline factory. [api/lambda_handler.py](../src/Sentiment_analysis/api/lambda_handler.py)
exports the Mangum handler for HTTP API payload format 2.0, with lifespan events
off. `config.serving_runtime.api_runtime()` wires only DynamoDB and S3 signing
clients through AWS roles. No queue registry or YouTube key is needed by the API.
Deployment must assign the table/auth/release settings and seed a verified model
manifest before protected routes can operate.

### Run the API checks

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-serving-test.txt
.\.venv\Scripts\python.exe -m unittest tests.serving.test_api -v
```

Tests use FastAPI's in-process [TestClient](https://fastapi.tiangolo.com/reference/testclient/)
with the pinned HTTP test client, mocked AWS and synthetic verified JWT contexts.
They check ownership, read/write scopes, unsafe bodies/headers, retries, cache
hits, downloads, deletion, deadlines, quota resets, dependency failures, CORS
and the actual HTTP API v2/Mangum response shape. Responses validate against both
the written contract and generated OpenAPI schemas; operation IDs, status codes,
retry-key parameters and security declarations are compared too.

The contract was clarified for malformed path UUIDs (`422`) and repeated owned
deletion (`204`); these now match the implementation. The test dependency file
stays separate from deployed requirements. These tests establish HTTP behavior,
not 1,000-user AWS capacity.

The fixture worker is implemented in the next section. The later sections
describe the gated live provider and implemented extension connection. Real
sign-in, an authorized live sample and AWS deployment remain verification work
in the [backend plan](backend-plan.md).

## Fixture worker and shared predictions

The API can now hand a fixture analysis to a worker and read the finished result.
This stage exercises that complete flow without a live YouTube request or a
deployed AWS service. Start with the synthetic pages in
[worker-fixture.json](../examples/worker-fixture.json): five received rows contain
four unique IDs because one comment appears on both pages.

### Configuration and model identity

The existing `model` and `worker` sections in `config/serving.yaml` control this
stage. `model.text_column` identifies the trained text column and is checked
against the loaded bundle. `worker.maximum_attempts` allows five processing
attempts across all dispatch generations; a sixth claim fails the job before
another fetch. This cap still works when recovery sends a new SQS message and
its receive count starts over.

[entity/worker.py](../src/Sentiment_analysis/entity/worker.py) defines the image
identity, provider page, captured comment and private checkpoint. A checkpoint
holds video metadata, page history, the current call's attempt count, pending
comments and completed predictions. Every transition revalidates counts, unique
IDs, timestamps and contiguous prediction indices. Oversized raw text becomes
`text_too_long` with no retained text before normalization or S3 checkpointing.

[config/worker.py](../src/Sentiment_analysis/config/worker.py) builds the prediction
configuration after checking the assigned release, image code revision and
packaged training report. `PredictionService` then verifies the model checksum,
package versions, bundle metadata, preprocessing recipe, label mapping and text
column before prediction. A missing packaged model fails; it never triggers a
DVC download in the worker.

The shared service accepts `native_thread_limit=1` for serving. Omitting it keeps
the training report's original thread limit. The override changes no saved
parameter, preprocessing recipe or model weight. Batch size and review threshold
come from the serving settings. Each warm runtime loads one service and reuses
it across jobs; serving imports no MLflow or notebook stack.

### Fetch, checkpoint and infer

[components/analysis.py](../src/Sentiment_analysis/components/analysis.py) handles
deduplication, batches, index matching and result assembly.
[pipeline/analysis.py](../src/Sentiment_analysis/pipeline/analysis.py) handles
leases and I/O in this order:

1. Check the message release and conditionally claim the queued job.
2. Restore and validate the last published checkpoint against accepted work.
3. Spend one global allowance, save the logical call's attempt counter, then
   fetch metadata or a page. A crash before the fetch may waste an allowance;
   it cannot create a fetch outside the allowance or retry limit.
4. Save each accepted page, its next token and progress under the current lease.
5. Finish the bounded raw sample, then predict each pending batch and checkpoint
   its completed rows and updated counts.
6. Enter `SAVING`, upload the validated result and conditionally publish its
   immutable pointer. Only that terminal transaction releases active capacity
   and the unused reservation.

Duplicate IDs are removed in first-seen order. They still count toward the raw
requested limit, so fetching never refills them. Each call requests the smaller
of page size and remaining raw count. Page count is bounded by
`ceil(requested/page_size)`; a short-page sequence that reaches that bound has
an explicit partial reason. Repeated page tokens fail instead of looping.
Reaching the raw limit normally succeeds with `has_more=true` when appropriate.

Predictions are joined by returned input index, even if returned in another
order. Processed rows move out of `pending` and into `predictions`, preserving
their original result indices. Empty text, unknown vocabulary and oversized
text remain explicit skips. Counts reconcile against the actual rows; coverage
is analyzed divided by unique received. Confidence remains the highest class
score, with no claim that it measures calibrated correctness.

### Failures and recovery

A transient provider failure has at most three attempts for that logical call,
including across crashes. Retries use bounded jittered delays. Exhaustion after
some accepted comments produces `PARTIAL`; exhaustion with no sample produces
`FAILED`. Missing/nonpublic videos and disabled comments fail explicitly.
An empty public sample succeeds with zero rows. A provider error message is
never copied into the public failure or logs.

The worker checks the overall deadline, current lease and invocation time around
operations. At the soft limit, or with insufficient Lambda time remaining, it
requeues through the durable outbox while keeping the existing checkpoint and
overall deadline. Global fetch throttling also yields before a provider call.
Storage/model runtime failures request bounded recovery. Invalid checkpoints,
invalid prediction rows and byte-limit violations fail explicitly.

Heartbeats occur between bounded calls and batches. The checkpoint object is
uploaded before its pointer and progress are committed together. A crash after
upload can leave an orphan; recovery uses only the published pointer. A crash
after inference publication resumes the remaining batch, while a crash after
result upload can republish from completed predictions without refetching.
A stale worker uses its own old token and cannot recover or replace a newer
worker's lease. S3 uploads alone never make a result available.

### Lambda entry point and checks

[workers/analyze.py](../src/Sentiment_analysis/workers/analyze.py) exposes the SQS
handler. Failed initialization or unhandled records return
`batchItemFailures`, using the SQS message IDs. Deployment must enable
[ReportBatchItemFailures](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)
and use the configured batch size of one. An acknowledged recoverable job already
has a durable recovery outbox record; failed recovery leaves the record retryable.
Malformed or misrouted records are not silently accepted.

[config/worker_runtime.py](../src/Sentiment_analysis/config/worker_runtime.py)
creates the warm pipeline lazily. Importing the handler loads neither the model
nor AWS clients. Its development fixture mode requires an explicit
`SERVING_FIXTURE_PATH`, assigned serving/table settings and a trusted
`SERVING_IMAGE_CODE_REVISION` matching the packaged code. It uses AWS roles/default
credential resolution, rather than the local DVC credentials. This Lambda factory
is not a local AWS emulator; the tests below provide the emulator.

Production refuses fixture providers. No live adapter is wired yet, and the
development YAML still has no assigned release/table. Tests derive synthetic
release identities in memory; they do not promote a deployed model or change
the development YAML to appear deployment-ready.

The deployed worker dependencies are in
[requirements-worker.txt](../requirements-worker.txt). Moto is test-only.
Run the storage/API/worker protocol suite with:

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-serving-test.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests/serving -v
```

The real-model checks are in `tests/test_worker_model.py` and
`tests/test_prediction.py`. They train a tiny local fixture, compare worker rows
to the direct shared predictor, check bundle guards and verify one model load
across successive jobs. Run them in an environment where the pinned native
packages can load:

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-worker.txt -r requirements-serving-test.txt -r requirements-eda.txt
.\.venv\Scripts\python.exe -m unittest tests.test_prediction tests.test_worker_model -v
```

GitHub's Linux CI runs those checks and the full repository suite. Windows
Application Control currently blocks this machine's scikit-learn DLL; local
protocol tests use synthetic predictions. These checks establish worker behavior
and shared-model integration, not production capacity or live YouTube accuracy.

The next section implements auth infrastructure and operational metrics. The
live provider implementation is covered below. Connected extension sign-in and
measured 1,000-user AWS deployment remain later steps in the backend plan.

## Authentication, quota controls and monitoring

Authentication has two jobs here: Cognito identifies the person, and the gateway
checks that their access token may use a route. Our API then checks the verified
context and uses the Cognito subject as the owner. Quota storage separately
decides whether a request or provider attempt fits the configured limits.

### Auth settings and infrastructure

[auth-infrastructure.yaml](../config/auth-infrastructure.yaml) contains the
deployment label, region, domain prefix and token lifetimes. It contains no AWS
key, client secret, real extension ID or invented deployed resource ID.
[entity/auth.py](../src/Sentiment_analysis/entity/auth.py) validates it, and
[config/auth.py](../src/Sentiment_analysis/config/auth.py) loads it with the same
duplicate-key protection as serving YAML.

[infra/auth_stack.py](../infra/auth_stack.py) defines a retained Cognito user
pool with email sign-in/verification, custom `analysis/read` and `analysis/write`
scopes, and a public app client. Only authorization-code OAuth is enabled;
implicit and client-credentials grants are absent. Password/SRP SDK flows are
not enabled on this client. Access and ID tokens last 15 minutes, refresh tokens
seven days, and token revocation is enabled.

The real `ExtensionId` is a required CloudFormation parameter. Its callback is
exactly `https://<id>.chromiumapp.org/cognito`, matching
`chrome.identity.getRedirectURL("cognito")`; logout uses `/logout` on that same
origin. The Cognito domain prefix appends the AWS account ID. API function
`name[:alias]` is another required parameter, and its ARN is constructed in the
stack account and region. No default fixture ID or cross-account function is
silently selected.

All five `/v1` routes use the JWT authorizer and their individual read/write
scope. Only `/health/live` is public. There is no catch-all integration or public
Swagger route. Lambda invoke permissions are restricted to this gateway,
account and route methods. The existing Lambda must also have no alternate
public function URL or unintended direct invoker; the template cannot inspect
another deployment's existing permissions.

The gateway uses the serving configuration's overall and POST-specific
throttles. These gateway limits complement the authoritative application
counters; they do not replace per-user admission or daily quota transactions.
CORS has exactly the declared extension origin. API Gateway verifies JWT
signatures/issuer/audience and required scopes, while the API additionally
requires access-token use, the configured public client, expiry and owner UUID.
See the [AWS JWT authorizer documentation](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html).

### S256 in the extension

Cognito supports S256 when the authorization request supplies a challenge;
its app-client configuration does not have a general require-PKCE switch. We
therefore require PKCE in our own
[auth-pkce.mjs](../extension/auth-pkce.mjs) request builder. Every request has a
fresh 32-byte random verifier and state, `response_type=code`, and
`code_challenge_method=S256`. The verifier never enters the authorization URL.
This follows [Cognito's authorization flow](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html)
and [RFC 7636](https://datatracker.ietf.org/doc/html/rfc7636).

The callback checker requires the exact HTTPS origin/path, matching state,
one code, no duplicate fields, no implicit token or fragment, and an unexpired
attempt. It consumes the pending attempt, clears the saved verifier and returns
a form-encoded exchange descriptor for the trusted Cognito token endpoint.
Redirects and browser credentials are disabled on that exchange descriptor.
Provider errors remain generic; raw callback URLs and error descriptions are
never logged by this helper.

The helper performs no network request or storage write. Step 7's
`AuthSession` now calls it after a user clicks Sign in, keeps the pending
verifier in memory and stores only the access token, expiry and nonsecret
revision in trusted extension session storage. It handles expiry and uses
access tokens for the API. Typing an arbitrary bearer string into standalone
Swagger still does not create a trusted gateway context. The checked-in
settings and an unconfigured public website stay in demo mode; a generated enabled extension
receives the exact permissions needed by its assigned deployment.

### Metrics without comment or identity data

[entity/telemetry.py](../src/Sentiment_analysis/entity/telemetry.py) permits only
fixed event, outcome, route and error labels, numerical status/attempt values,
opaque request/work UUIDs and a validated release fingerprint. It has no field for a comment, author, email,
owner, credential, token, request body, full path, provider error text or signed URL.
Rejected fields/metric names never reach the output sink.

[services/telemetry.py](../src/Sentiment_analysis/services/telemetry.py) emits
CloudWatch [embedded metric format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html)
as JSON on stdout, without an AWS SDK call. Namespace is `CommentLens/Serving`;
dimensions are only `Environment` and `Service`. Release/request/work IDs remain log
fields rather than metric dimensions, keeping metric cardinality bounded.
Unknown event/error labels, booleans used as counts, negative values and
nonfinite numbers are rejected. A broken log sink does not change admission,
quota spending or job publication.

| Component | What it records |
| --- | --- |
| API middleware | Request count/latency, safe route template, status, auth/rate/dependency denials |
| Job repository | New work, replay/join/cache outcomes, reserved and spent units, quota denials, terminal counts and duration |
| Worker | Accepted, duplicate and misrouted deliveries; recoverable dependency failures |
| Dispatcher/reconciler | Dispatch attempts/failures and recovery/cleanup/error totals |

Spending and terminal metrics emit after successful conditional transactions.
Heartbeats spend no provider units, and duplicate terminal delivery creates no
second terminal count. `YouTubeAttemptUnits` means charged allowances, including
an allowance wasted by a crash before the provider request. Logs are best effort;
a crash between a database commit and emission can lose a metric. DynamoDB's
spent/reserved counters remain the quota source of truth, not CloudWatch totals.

API metrics use route templates or `other`, never the requested path/query.
Requests rejected by the gateway never enter Lambda; gateway 4xx/throttle
metrics and access logs cover them, while application auth counters cover
denials that reach the app.
Gateway access logs include request ID, matched route, status, response bytes
and integration latency; they exclude IP, headers, claims and raw error text.
The auth stack retains those logs for 14 days and defines alarms for gateway
5xx errors, API dependencies, quota denials, provider circuit openings and failed jobs. Alarm notification
destinations and the worker/queue/Lambda operational alarms are deployment work.
Existing generic warning messages still omit exception text.

During deployment, use text-format Lambda logging so emitted EMF stays at the
log record root, grant normal scoped log-write permissions, and apply the
14-day retention policy to each Lambda log group. CloudWatch creates the custom
metrics from these records; no application `PutMetricData` permission is needed.
Live cost/volume measurements still determine alarm thresholds and concurrency.

### Synthesize and verify

Install the infrastructure tools separately from deployed requirements:

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-infra.txt
.\.venv\Scripts\python.exe -m infra.app
.\.venv\Scripts\python.exe -m unittest tests.infra.test_auth_stack tests.serving.test_telemetry -v
node --test extension/tests/lib.test.mjs extension/tests/auth-pkce.test.mjs
```

Synthesis writes `artifacts/auth-infrastructure/CommentLensAuth-development.template.json`
and a validation report. It performs no AWS context lookup, bootstrap, deployment,
user creation or cost-incurring resource operation. Outputs describe the future
issuer, public client, login/token endpoints, callback and API URL; they are
CloudFormation expressions until deployment.

Template checks cover every route's scope, public-client grant restrictions,
exact callbacks, retention, CORS, invoke permissions, alarm dimensions and
throttle serialization. JavaScript checks include the RFC S256 vector, fresh
random values, replay/state/expiry failures and safe exchange destinations.
Serving checks cover privacy, sink failures and unchanged transaction behavior.
Linux CI also runs the full model pipeline and extension suite.

Full-suite discovery uses `python -m unittest discover -s tests -t . -v`.
The explicit repository top level keeps `tests.infra` distinct from the
application's `infra` package.

Step 5's infrastructure and step 7's sign-in connection are implemented and
fixture-tested. Real Cognito sign-in is not deployed or verified.
Before deployment, supply the stable extension ID and existing API function,
copy real public outputs into serving/extension settings, audit invocation
permissions, configure log/notification delivery and verify sign-in with a real
Cognito user. Verify email-delivery/service quotas for the launch target as part
of deployment. Live YouTube analysis stays disabled until its approval/quota
references and deployment prerequisites are recorded.

## YouTube provider and live readiness

The worker can now use YouTube Data API v3. This changes the source of comments;
the shared predictor, result contract, owned requests and private storage still
follow the same path as the fixture worker.

Start with `youtube` in [serving.yaml](../config/serving.yaml). It sets public
visibility, latest-first top-level comments, plain text and a maximum of 1,000
received comments. HTTP connect/read timeouts are 3/5 seconds, the request/body
deadline is 12 seconds and one response is capped at 2 MiB. The typed settings
require these timeouts to fit the deadline, and the deadline to fit between
worker heartbeats. These are initial bounds to measure during deployment.

### Configuration and the API key

[config/youtube.py](../src/Sentiment_analysis/config/youtube.py) revalidates typed
settings before creating live clients. `live_enabled` requires recorded
analytics-use approval, a nonblank approval reference, a regional Secrets
Manager ARN and verified project quota. A copied settings object cannot skip
those checks. The checked-in secret ARN points to the project's `us-east-1`
secret; live fetching stays disabled while analytics-use approval remains unverified.

The secret ARN is recorded in serving YAML. Its contents and worker-role access
have not been verified from this workstation, which has no configured AWS role
or profile.

`youtube.project_id` records `applied-polymer-511605-m3` as the Google Cloud
project supplied for this setup. On 2026-10-14, the user-provided Google Cloud
quota screenshot showed `youtube.googleapis.com` enabled, general **Queries per
day = 10,000**, and current usage of zero. That evidence records
`project_quota_verified: true` with a 10,000-unit daily allocation. The existing
80% working allowance remains 8,000 units. The separate search and other method
quota rows are not added to this budget.

This screenshot does not verify the key's project association or analytics-use
approval. The approval reference is still pending. Quota verification records
the console evidence, without an authenticated Google Cloud query from this
workstation; recheck the project's allocation before production deployment.

Record the approval reference before enabling live fetching. To collect or
update these details:

1. Record the reference to YouTube's accepted analytics-use decision for this
   project and application. It may be a case ID, an official decision link, or
   the date/subject of approval correspondence with a private evidence reference.
   The [derived-metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy)
   covers aggregate comment sentiment under the accepted analytics amendment.
2. Select the matching Google Cloud project, then **APIs & Services → YouTube
   Data API v3 → Quotas & System Limits**. Record the project ID, actual daily
   allocation and verification date. A key alone does not establish analytics
   approval or the project's allocation.
3. In AWS Secrets Manager, select `us-east-1`, choose **Other type of secret**,
   and store one key/value pair named `api_key`. For example, name the secret
   `comment-lens/youtube-api-key`. Copy its ARN from the details page. Follow
   [AWS's secret creation instructions](https://docs.aws.amazon.com/secretsmanager/latest/userguide/create_secret.html).
   Send the ARN, never the key value.

The secret loader accepts a plain API key or exactly `{"api_key": "..."}`. It
rejects binary secrets, duplicate/extra JSON fields, invalid keys and values
over 4 KiB. Errors use fixed messages without secret contents. Boto3 uses the
default AWS execution role/profile; runtime code reads no DVC credentials or
environment variable containing a YouTube key. Deployment grants the worker
`secretsmanager:GetSecretValue` on that one ARN and, for a customer-managed key,
the required scoped KMS decrypt permission.

[worker_runtime.py](../src/Sentiment_analysis/config/worker_runtime.py) selects
an explicit `SERVING_FIXTURE_PATH` only in development. Otherwise it requires
the live settings, reads the secret and builds the live provider. It never
silently switches to demo data. The cached factory reuses the key/provider and
one verified prediction service per warm worker. Importing the Lambda entry
point loads no model or AWS clients.

### Record analytics approval

The developer reported submitting the audit on **2026-10-15**. No accepted
analytics-use decision or case reference has been supplied. Keep the submitted
evidence and review correspondence, and record the accepted decision when it
arrives. Submission does not change `analytics_use_approved: false`,
`approval_reference: null` or `live_enabled: false` in `config/serving.yaml`.

If approval has already been granted, record the decision reference in
`youtube.approval_reference` and verify that it covers this project's comment
sentiment use case. There is no mandatory case-ID format in the published
process. An email date/subject plus a private evidence reference is useful when
the decision has no case number. Record accepted-use evidence rather than an
application receipt, API-enabled status or credentials page.

For an application that still needs approval, open the
[YouTube API audit form](https://support.google.com/youtube/contact/yt_api_form?hl=en).
Find Section 5 and select **Analytics & Reporting**. Review the derived-metrics
amendment and provide the requested application evidence. YouTube determines
whether the use case qualifies; selecting its acknowledgement does not itself
grant approval. This flow is described in the
[derived-metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy).

The form asks for the numeric Google Cloud **project number**, available beside
the project ID on the Cloud dashboard. It also asks for a public HTTPS privacy
policy and dashboard evidence. The project ID recorded here is
`applied-polymer-511605-m3`; `youtube.project_number` records `170746039104`
from the user's pasted audit form. Confirm their association when reviewing or
amending the submitted evidence. The static demo and legal pages are now published
on GitHub Pages; their HTTPS pages and assets were verified on 2026-10-14. Use
the exact URLs below and label preview screenshots as demo data.

For the pasted Sections 4 and 5, use these values:

| Field | Value for this design |
| --- | --- |
| API client name | Comment Lens |
| Client name contains YouTube | No |
| Publicly accessible | Yes, the static demo; the live backend is not deployed |
| Primary Access URL | https://siddharthaganguli.github.io/youtube_Comment_analyzer/ |
| Privacy Policy URL | https://siddharthaganguli.github.io/youtube_Comment_analyzer/privacy.html |
| Terms of Service URL | https://siddharthaganguli.github.io/youtube_Comment_analyzer/terms.html |
| Dashboard review URL | https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html |
| Number of projects | 1 |
| Google Cloud project number | `170746039104`, as supplied |
| Use case | Analytics & Reporting |
| Google-account OAuth | No; public YouTube reads use an API key, while application login is planned through Cognito |
| Expected API usage | Current live usage is zero; the planned working ceiling is 8,000 attempted units/day including retries |
| Read endpoints | `videos.list` and `commentThreads.list` |
| Quota amount | No change / Default quota (10k quota points) |

The usage dropdown choices were not included in the pasted form. Match its
displayed units/range to expected API use; the 1,000-user target is not an API
request estimate. A configured ceiling is also not a measured daily forecast.

The privacy policy is published as [an HTML page](https://siddharthaganguli.github.io/youtube_Comment_analyzer/privacy.html), with
[a separate upload PDF](../output/pdf/comment-lens-privacy-policy.pdf). The Terms
of Service are published as [an HTML page](https://siddharthaganguli.github.io/youtube_Comment_analyzer/terms.html), with
[a separate upload PDF](../output/pdf/comment-lens-terms-of-service.pdf). The developer
confirmed `siddharthaganguli0093@gmail.com` as the public contact.
Popup and dashboard footers link to both pages. The privacy policy explains current preview
behavior separately from the undeployed live service, including public comment
data, cloud processing, shared caches, expiration and email deletion requests.
The privacy PDF is for the first evidence upload; the terms PDF is for
**Terms of Service Documentation**, the third upload. Neither is a public URL.
The terms include the YouTube Terms of Service agreement and explain model
limitations, responsible use, content rights and result expiration.
The static demo makes no YouTube Data API or Comment Lens backend calls. GitHub
receives page/asset requests and logs visitor IP addresses for security; opening
or reloading a dashboard URL containing a selected video link sends that URL to
GitHub too. The privacy policy identifies this hosting behavior and separate
provider retention. Step 7 implements the extension's consent/session controls;
their native Chrome behavior and real account/deployment flow remain pending.

The [published demo homepage](https://siddharthaganguli.github.io/youtube_Comment_analyzer/) now shows the Privacy Policy link
and a separate official YouTube branding block. It links to the demo and legal
pages, and those pages link back Home. The extension name is now **Comment Lens**
in its manifest. [The page preview](../output/evidence/homepage-preview.jpg)
captures the published homepage content; its capture API does not include Chrome's
address bar, so it is not the final audit screenshot.

For the Homepage Screenshot box, use Windows Snipping Tool to capture the actual
Chrome window with the address bar, policy link, logo and demo notice visible.
The [audit form](https://support.google.com/youtube/contact/yt_api_form?hl=en)
asks for readable evidence at least 1280x720 pixels, including the browser
address bar, and one file smaller than 10 MB. Open the published Homepage URL
above for the final capture so its real HTTPS address is visible.

The public HTTPS app/policy URLs are available. Retain the genuine window
captures and documents used in the reported audit submission; page-content
previews do not replace screenshots with an address bar. Publication does not
grant YouTube analytics-use approval or deploy the AWS backend; both remain
pending.

The [Pages workflow](../.github/workflows/pages.yaml) checks the extension's
JavaScript suite, stages an explicit static-file allowlist with
`scripts/build_pages_site.py`, and deploys `_site` through GitHub Pages. Only
the homepage/dashboard/legal HTML, their CSS and demo dashboard modules, the
official logo and `.nojekyll` are published. Extension account/API/session and
fixture modules, connection settings, backend code, credentials, datasets and
model artifacts are excluded from the site artifact.

This draft describes the current implementation and proposed live behavior:

> Comment Lens is a Chrome extension in development, with a popup and a full
> dashboard. Its proposed live backend retrieves public video metadata and up
> to 1,000 latest top-level comments through videos.list and commentThreads.list.
> A TF-IDF and logistic-regression model estimates Positive, Neutral and Negative
> sentiment from the sampled text. The dashboard presents aggregate estimates,
> coverage and model confidence separately from YouTube-provided statistics.
> The current preview uses handwritten demo comments; the live service is not
> deployed. The design uses Cognito for our application's login and an API key
> for public YouTube reads. It does not request private YouTube data or infer
> protected attributes. Serving checkpoints and results use private S3 storage
> with a 24-hour retention policy. Users can remove their owned request
> references; shared cached analyses expire under that retention policy.

Review the draft against the application's actual state when preparing the
submission. Keep `analytics_use_approved` and `live_enabled` false until the
accepted decision is recorded and the remaining serving prerequisites are ready.

### What the adapter requests

[integrations/youtube.py](../src/Sentiment_analysis/integrations/youtube.py)
uses verified TLS to the fixed `www.googleapis.com` origin. It requests only
`/youtube/v3/videos` and `/youtube/v3/commentThreads`, using a validated video
ID and opaque page token rather than a user-provided URL. There are no redirects,
ambient proxies or transport retries. The API key is sent in the
[`X-Goog-Api-Key` header](https://docs.cloud.google.com/apis/docs/system-parameters),
outside the URL. Transport diagnostics and raw error bodies never enter logs.

The metadata response must match the requested ID and have
`status.privacyStatus=public`. Private/unlisted videos fail explicitly; a
missing video gives `VIDEO_NOT_FOUND`. Comment requests use `order=time`,
`textFormat=plainText` and at most 100 threads per page. The adapter reads
`topLevelComment.snippet.textDisplay`; `textOriginal` is not reliably available
to non-authors. Replies are excluded. Duplicate IDs are preserved until the
worker counts/removes them, and the worker retains its existing sample/page
ceilings rather than fetching extra pages to replace duplicates. See the
[comment resource](https://developers.google.com/youtube/v3/docs/comments) and
[commentThreads.list](https://developers.google.com/youtube/v3/docs/commentThreads/list).

Malformed identities, dates, counts, tokens, JSON, unexpected replies, oversized
bodies and invalid response shapes fail with safe fixed codes. Empty valid
pages remain valid input and can produce an empty result. Network errors,
429 and 5xx responses are retryable; disabled comments, missing/nonpublic
videos and invalid response bodies have explicit terminal outcomes.

Socket timeouts and a monotonic deadline bound connection/request/body reads.
The standard-library DNS resolver has no interruptible timeout here; a stuck
OS DNS lookup is bounded by the outer Lambda timeout and fenced recovery,
not a guaranteed 12-second total wall-clock return. Measure DNS/network behavior
in the deployed environment before setting production targets.

### Retries, visibility and the shared circuit

The pipeline spends an allowance and saves the logical call counter before each
HTTP attempt, including visibility checks. Each metadata/page call has at most
three attempts across recovery. Transport failure never refunds a spent unit.
The initial maximum-size reservation is 33 units; a resumed worker may need an
extra visibility call, which must fit the same authoritative daily budget.
[YouTube's quota table](https://developers.google.com/youtube/v3/determine_quota_cost)
lists a one-unit cost for these methods and charges invalid attempts too.

Short retries use bounded jitter. A valid `Retry-After` is retained, including
delays longer than 60 seconds, and saved as `retry_not_before`. A worker waits
only if the delay and the next HTTP call fit its invocation and job deadline;
otherwise it releases its lease into a delayed outbox generation. The stream
dispatcher leaves that record pending until its timestamp, and the scheduled
reconciler eventually sends it. Premature queue duplicates cannot claim a new
lease. A delay beyond the overall deadline ends with deadline expiry, without
another provider call. Retry delays are capped at 24 hours, longer than every
permitted job lifetime. Hard crashes can cause conservative extra allowance
spending, while durable counters still bound actual retries.

On every new live lease with saved metadata, the worker spends an allowance to
check the video again before using saved comments or predictions. This also
applies when fetching was complete. A video that became private or unavailable
fails without publishing a result. Visibility retries have a separate durable
counter, so they do not reset an interrupted page's attempts. A successful
recheck preserves the original sample fetch timestamp. Private checkpoints
remain under the existing 24-hour serving cleanup policy.

[provider_circuit.py](../src/Sentiment_analysis/repositories/provider_circuit.py)
stores one `CIRCUIT#YOUTUBE` item in the serving table. Verified Google quota
faults block new work and future provider attempts until Pacific midnight;
credential/service-configuration faults block until operator repair. Admission
and spending pin the circuit version in their DynamoDB transaction, so an
opening between the read and commit cannot create new work or spend unnoticed.
Existing cache hits, joins, idempotent replays and owned reads remain available.
Requests already in flight cannot be cancelled by the circuit.

A quota fault after a usable, publicly checked sample can publish a clearly
marked partial result. Credential faults fail explicitly. A failed visibility
recheck never publishes a partial result using unverified saved data. Circuit
opens/blocks emit allowlisted metrics; the auth infrastructure adds a
`ProviderCircuitOpenedCount` alarm. Notification routing remains deployment work.

Quota opens expire through the strongly read timestamp, independent of TTL.
Credential repair requires fixing the restricted key/service configuration,
recycling warm workers so they load the repaired secret, and then an operator
calling `close_after_credentials_repaired(expected_version=...)` with the
freshly read circuit version. There is no public reset route and no quota-reset
method; the action does not alter daily spent/reserved counters.

### Checks and the remaining live sample

All provider/runtime/circuit/worker tests use synthetic responses or in-process
AWS fixtures. Run them alongside the existing serving checks:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests/serving -t . -v
.\.venv\Scripts\python.exe -m unittest tests.infra.test_auth_stack -v
```

The adapter and worker wiring are implemented. The small live verification
has not run: analytics-use approval evidence, verification of the configured secret and
worker-role access, assigned
serving table and immutable worker release must be ready first. No serving
API/worker infrastructure has been deployed. Use the normal admitted job → outbox
→ worker path for that live sample so spending, ownership and retention are
tested together. Check metadata visibility, comment ordering, pagination,
actual attempt units, result counts, safe faults and eventual cleanup. The
extension's connection source is implemented below; its checked-in defaults
and public Pages site remain a demo.

## Extension connection and generated bundles

Step 7 connects the existing API contract to the dashboard. It is implemented
and checked with synthetic authentication/API responses and DOM fixtures.
The source does not contain deployed account or API settings. The public
website and Chrome extension each use their own validated public configuration
after the AWS deployment is ready. Website sign-in uses a full-page Cognito
redirect; extension sign-in keeps its registered Chromium callback.

This distinction helps when reading the files: the UI and request logic can be
exercised now, without setting `live_enabled` or spending YouTube quota. An
audit submission is still separate from accepted analytics-use evidence.

### Follow the data through the extension

| File | Responsibility |
| --- | --- |
| `extension/client-config.mjs` | Validate public URLs, IDs, the private result prefix and client limits |
| `extension/client-settings.mjs` | Disabled source default; the bundle builder writes assigned public values into the output |
| `extension/auth-pkce.mjs` | Fresh state/verifier, S256 and a one-use exact callback |
| `extension/auth-session.mjs` | User-initiated Cognito exchange and access-only trusted session |
| `extension/analysis-client.mjs` | Authenticated fixed API routes and bounded, verified private downloads |
| `extension/result-model.mjs` | Validate counts, model/sampling identity and comment states; adapt data for rendering |
| `extension/job-controller.mjs` | Own the visible request, poll, retry, expire, remove and fence stale responses |
| `extension/dashboard-app.mjs` | Render demo or connected state, consent controls, charts, table and export |
| `extension/dashboard.mjs` | Load connection modules only in a configured Chrome extension |

The popup still reads the active tab only when invoked. The connected UI asks
the user to read the Privacy Policy and accept the Terms before account access
or submission. Opening the dashboard does not start an interactive login or an
analysis. Demo mode remains available without signing in.

### What the session keeps

`AuthSession` checks that Chrome's runtime ID, extension URL and
`identity.getRedirectURL("cognito")` match the assigned extension. It restricts
`chrome.storage.session` to `TRUSTED_CONTEXTS`. The session record contains
only the access token, expiry time and a nonsecret sign-out revision. ID and
refresh tokens returned by Cognito are not retained. The pending verifier
stays in memory and is cleared when the attempt finishes or is cancelled.

Token response size, destination, JSON type, token use, client ID, scopes and
lifetime are checked. Decoding JWT claims here bounds client behavior; it is
not signature verification. API Gateway and the backend remain the authority
for signature, issuer, audience, expiry, scopes and owner access.

There is no refresh-token flow: the user signs in again when the access token
expires. Sign-out clears the extension session and changes a nonsecret
revision so another window's pending sign-in cannot restore it. Account
changes clear the old window's displayed results. This action does not call
Cognito's global logout, revoke an already issued JWT elsewhere, delete an
account or clear the provider's hosted-login cookies.

### Request and result lifecycle

A fresh submission gets a UUID retry key. If its response is lost, the
controller's Retry action reuses that key for the same remembered submission.
The API client never retries a POST automatically. A new submission gets a new
key, and a reloaded dashboard does not recover an unfinished submission from
persistent history.

Queued, fetching, inference and saving states use the API's actual progress.
Polling follows 2/5/10-second delays with jitter and honors server retry delays.
It pauses while the page is hidden. One visible request is tracked by this
controller; cancellation and a revision fence stop an old request or account
from repainting newer state. Transient polling failures have bounded retries.
Quota errors retain the reset time; busy and rate-limit errors retain the
suggested wait. Errors use fixed client copy, without echoing provider messages,
tokens or signed URLs.

When a terminal result is available, the client obtains its owned download
descriptor. It checks the exact configured S3 origin/private prefix, immutable
result path, expiry, byte ceiling, exact byte count, SHA-256 and result schema.
The S3 request receives no API bearer token or browser credentials. A 403 or
expired descriptor can be renewed once without creating another analysis.
The API's verified status is matched to the downloaded video's ID, model,
sampling, state and counts.

The result is kept in memory for charts, comments, search, filters and CSV;
changing a filter makes no API call. Positive/neutral/negative percentages use
analyzed comments. Empty, unknown-vocabulary and oversized-text skips remain
visible, and the review threshold comes from the result's model. Current-model
evaluation details are displayed only when their release matches the result.
The view uses the earlier owned-request expiry when a shared result lasts longer.

Partial, empty, all-skipped, failed, quota, busy, expired and deleted outcomes
have distinct states. Expiry, sign-out and account changes clear displayed
private results. Removing an analysis deletes the user's reference; shared
work/results keep the backend's normal retention, and previously issued
download links follow their existing short expiry. It does not cancel another
user's shared work or delete an account.

### Generate the package from public deployment values

Run from the repository root, with Python and a Node version supported by
`package.json`:

```powershell
.\.venv\Scripts\python.exe scripts/build_extension_bundle.py
```

The default output is `artifacts/extension`, an unpacked demo package with
`enabled: false` and only `activeTab` permission. Load that directory in
Chrome and keep it at that path when assigning its extension ID. Moving an
unpacked extension can change the ID; the package settings, Chrome's ID and
Cognito callback must match exactly.

After the API, Cognito and private serving bucket/prefix are deployed, provide
a flat public JSON file to the same builder:

```powershell
.\.venv\Scripts\python.exe scripts/build_extension_bundle.py --settings public-extension-settings.json
```

Its fields are `enabled`, `apiBaseUrl`, `cognitoDomain`, `clientId`,
`extensionId`, `resultBaseUrl`, optional `pollScheduleSeconds: [2, 5, 10]`,
and optional `maxComments` from 1 to 1,000. The
[extension guide](../extension/README.md#build-a-connected-extension-after-deployment)
describes each value. Use assigned AWS HTTPS origins and the actual Chrome ID;
placeholder endpoints are not a working production configuration. S3 uses its
assigned virtual-hosted bucket origin and a private `serving/development` or
`serving/production` prefix. API, Cognito and result storage must have distinct
origins.

An enabled generated manifest adds `identity`, `storage` and only those three
exact host origins. The packager validates public fields through the same
JavaScript module, copies an explicit runtime allowlist, and refuses linked
paths or an output directory containing unexpected files. `--output` can select
another empty or previously builder-owned directory. It never reads or copies
backend credential files, data/model artifacts or test fixtures. No deployment
or YouTube approval flag is changed by building a package.

### Exercise the connected screens locally

The fixture preview builder creates a separate, labeled developer artifact:

```powershell
.\.venv\Scripts\python.exe scripts/preview_extension_fixture.py
.\.venv\Scripts\python.exe -m http.server 8766 --bind 127.0.0.1 --directory artifacts/extension-fixture
```

Open `http://127.0.0.1:8766/dashboard.html?scenario=success`, accept the test
policy checkbox, choose **Use fixture account**, then **Simulate test run**.
The persistent developer-fixture banner states that there are no AWS or
YouTube calls. The scenario selector also offers partial, empty, all-skipped,
quota, busy, failed and expired responses. Normal polling advances the queued
examples, and the expiration case clears after 15 seconds. `--output` changes
the fixture destination; the builder does not launch the server. Fixture
modules are excluded from ordinary extension bundles and public Pages output.

This page has one fixed set of synthetic comments. Changing the video link
tests URL handling and result association; it cannot load that video's comments.
The fixture workspace, result details and comment totals remain labeled as
synthetic. The public demo's **Check video link** action only validates a link
and also keeps its handwritten comments. Neither page is a live backend.

Real comments require the configured API, authenticated account flow and
YouTube worker. The checked-in connection is disabled, the serving resources
are not deployed, and the local fixture server is only a static file server.
Rebuilding the preview or changing its default video ID does not connect those
services. Test results from this preview must not be reported as a live fetch.

Run the automated checks from the repository root:

```powershell
npm ci
npm test
.\.venv\Scripts\python.exe -m unittest tests.test_extension_bundle -v
```

`package-lock.json` pins the development test dependencies. The Node suite
covers PKCE/session handling, cancellation/account races, client settings,
download safety, result/count invariants, controller states, filters/CSV and
DOM fixtures. The Python bundle tests check generated settings, exact
permissions, allowlisted files and safe output replacement. These fixtures do
not prove native Chrome behavior, production access control or capacity.

### What still needs a real environment

On 2026-10-15, the fixture flow was checked in the IDE's integrated browser:
fixture sign-in, queued polling and result display completed for the selected
video, with two analyzed comments and one skipped row. This check exposed a
browser timer binding bug that Node's own timers had not caught. The controller
now calls default timers through `globalThis`; regression checks enforce the
browser receiver and preserve injected test timers. This is a fixture check,
with no real account authentication or YouTube requests.

The current native Chrome verification was blocked by the browser control's
URL safety check. No new native Chrome visual, identity or real Cognito check
is claimed for this stage. Repeat desktop/mobile layout, keyboard/focus,
accessibility, CSV/download and native identity/session checks against the
generated package. Verify the stable extension ID, exact callback, scopes,
host permissions and API/S3 CORS in the deployed environment.

The accepted analytics-use decision and its reference, authorized live sample,
AWS serving deployment, budgets/alarms, release promotion/rollback and
1,000-user load/fault checks remain open in the [backend plan](backend-plan.md).
The user-reported audit submission on 2026-10-15 leaves approval pending.
Checked-in serving approval/live flags and client connection defaults remain
disabled.

## Website connection and first AWS deployment

The ordinary dashboard can now call the same authenticated job API as the
extension. `web-auth-session.mjs` uses Cognito authorization code flow with
S256 PKCE. Only a five-minute, single-use state/verifier record survives the
redirect in this tab's session storage. Access tokens stay in memory; refresh
and ID tokens are not retained. Callback credentials are removed from browser
history before exchanging the code. Reloading requires sign-in again.

`web-client-config.mjs` validates website callbacks separately from Chromium
callbacks. The configured callback must match the dashboard's exact origin
and path. GitHub Pages uses
`https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html`;
development also registers the exact `127.0.0.1:8766` and `localhost:8766`
dashboard URLs. API and private result downloads require HTTPS. The YouTube
key stays in Secrets Manager and is read only by the analysis worker's role.

A configured website opens the analysis workspace with no sample results.
Sign-in and submission require the policy checkbox. Errors stay visible in
that workspace; they never replace a failed real request with demo comments.
**Explore demo** remains an explicit choice. Without deployed settings, the
page explains that live analysis is unavailable and labels its sample data.

### Prepare AWS permissions

The deployment identity needs permissions to create the named development
stack and its resources. An active access key only authenticates the caller;
the two deployment policies below grant those operations.

An account administrator must create these customer-managed policies from the
JSON files in `config/`:

| Policy name | JSON file | Assignment |
| --- | --- | --- |
| `CommentLensServingDevelopmentRuntimeBoundary` | `aws-serving-runtime-boundary.json` | Create it; the template assigns it as every Lambda role's permissions boundary |
| `CommentLensServingDevelopmentDeployment` | `aws-serving-deployment-policy.json` | Attach to the deployment user or role |
| `CommentLensServingDevelopmentWebDeployment` | `aws-serving-web-deployment-policy.json` | Attach to the deployment user or role |

In the IAM console, choose **Policies → Create policy → JSON**, paste one
file, then save it with the name above. Repeat for the other files. Under
**Users → yt-analyzer → Add permissions → Attach policies directly**, attach
the two deployment policies. These are customer-managed policies rather than
inline user policies because of IAM's different size limits. The files target
account `895812756022`, region `us-east-1`, and the development project names.
The runtime boundary is for the Lambda roles. Leave the deployment user's
**Permissions boundary** section **Not set**; assigning the runtime boundary
to that user blocks deployment operations.

If a managed policy already exists, open **Edit ? JSON**, replace its complete
document with the current repository file, and save the new version as the
**default version**. Keep the policy attached to the deployment user. The web
policy includes API Gateway stage tagging and Cognito create-time tagging;
CloudFormation checks those permissions before the final resource IDs exist.

The operator policies cannot change or detach the runtime boundary. Lambda
role creation and inline-policy updates require that boundary. The separate
Lambda roles restrict API, worker, delivery, cleanup and offline verification
access. Deployment uses an AWS role/profile; no credential value belongs in
website settings, source control, a Lambda environment variable, or chat.

### Build and deploy one immutable development release

Commit the source before packaging. The packager refuses uncommitted backend changes,
verifies the trained model checksum and builds separate Linux x86_64 Python
3.12 ZIPs. It installs Linux wheels rather than copying Windows packages and
rejects a package over Lambda's 250 MiB uncompressed limit. Training datasets,
DVC credentials, notebooks and MLflow are not included. Golden examples come
from the existing handwritten prediction check, with their model binding.

```powershell
.\.venv\Scripts\python.exe scripts/package_serving.py --output artifacts/serving-release-001
.\.venv\Scripts\python.exe -m infra.serving_app
.\.venv\Scripts\python.exe scripts/deploy_serving.py --release artifacts/serving-release-001 --template artifacts/serving-infrastructure/CommentLensServing-development.template.json --profile YOUR_DEPLOYMENT_PROFILE --deploy
```

The template creates the development table/indexes, encrypted private result
bucket, release queue/DLQs, scheduled recovery, Cognito client and HTTP API,
plus five Lambda functions. It uses no VPC/NAT or always-running server.
The existing DVC bucket is used only for private deployment packages; serving
results have their own bucket. The template exceeds CloudFormation's inline
template limit, so the script uploads it to the private artifact prefix.
It does not require a CDK bootstrap stack or SSM bootstrap parameter. Failed
creates preserve completed resources. After fixing an external permission
failure, rerunning the same release resumes its previous template and
parameters instead of deleting the named table, bucket or queues. A failed
stack cannot publish website settings or a current-model pointer.

After CloudFormation succeeds, the script invokes the verifier Lambda on Linux
before registering and promoting the release. That check compares the exact
packaged model's predictions with the saved golden results without calling
YouTube or reading its key. Verification failure leaves the current-model
pointer and website settings untouched. Replacing a different active release
is refused until the separate drain/promotion workflow is implemented.

Live YouTube processing remains disabled in the default template. Enabling it
requires the accepted analytics-use reference and a template synthesized with
`--enable-live-youtube`; provide that reference with `--approval-reference` at
deployment. A submitted audit alone does not change those settings. The default
API returns the explicit disabled error instead of enqueuing work it cannot run.

### Publish the configured website and run it locally

Successful deployment writes only public identifiers to
`config/web-client.public.json`: API URL, Cognito public client/domain,
registered dashboard callback and private result-store base URL. Commit and
push this file to publish through the Pages workflow. It passes the file to:

```powershell
.\.venv\Scripts\python.exe scripts/build_pages_site.py --settings config/web-client.public.json
```

An enabled build includes the website auth/client/controller modules from an
explicit allowlist. Extension session storage, tests, fixtures, backend code,
credential files, data and model artifacts stay excluded. Returning to disabled
settings removes previously allowlisted connection modules from the output.

For local testing, use the generated local callback settings from the release:

```powershell
.\.venv\Scripts\python.exe scripts/build_pages_site.py --settings artifacts/serving-release-001/web-client.local.json --output artifacts/serving-web
.\.venv\Scripts\python.exe -m http.server 8766 --bind 127.0.0.1 --directory artifacts/serving-web
```

Stop the older fixture HTTP server before starting this server on the same
port. `artifacts/extension-fixture` is always synthetic; the website build above
is the one that uses the real API. A real account sign-in and bounded live video
sample still need verification after AWS permissions, deployment and the
recorded live-use prerequisites are in place. No load/capacity result is implied
by the connection tests.
