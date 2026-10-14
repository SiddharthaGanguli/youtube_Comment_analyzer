# Backend implementation plan

The [system design](system-design.md) sets the launch target and its limits.
This checklist turns that design into small reviewable steps. Steps 1 through 5 are
implemented and checked with offline fixtures. Step 6's adapter and worker
integration are implemented and project quota is recorded; approval evidence and its live sample remain
pending, along with extension connection and deployment. Finish and verify each step, then commit and push it before
starting the next, following the
project's existing workflow.

Keep the offline training pipeline intact. Serving gets its own typed settings
so a change to polling or worker concurrency does not invalidate DVC training.
Use the [settings example](serving-settings.example.yaml) as the starting point,
and the [OpenAPI contract](backend-contract.yaml) as the interface to implement.

## 1. Serving configuration and entities

- [x] Add serving YAML, typed entities, then a serving configuration manager.
- [x] Validate cross-field rules: visibility >= 6 * timeout, lease > timeout,
  total mapping concurrency <= reserved concurrency, soft deadline < timeout,
  sample/page bounds, and freshness < retention.
- [x] Require model fingerprints and assigned release/code fingerprints for
  production, public-only scope, exact allowed origins, secret references and
  no embedded secret values.
- [x] Keep live YouTube analysis disabled without recorded analytics-use approval.
- [x] Define typed request/status/result/error entities matching the contract.

The [serving configuration walkthrough](serving-configuration.md) explains the
files, local validation command and the development/production distinction.
Contract/configuration checks run without AWS or the dataset.

Suggested commit: `feat: add typed serving configuration and API entities`.

## 2. Durable job and storage components

- [x] Add DynamoDB repositories for owned requests, conditional cache claims,
  idempotency, fenced work leases, quota counters, active slots and the outbox.
- [x] Add private S3 checkpoint/result components with immutable attempt keys,
  hashes, byte ceilings and expiry-aware download signing.
- [x] Implement transactional admission/terminal release; reclaim stale slots
  without depending on TTL deletion.
- [x] Add dispatcher and scheduled reconciliation for pending outbox, expired
  leases, deadlines and data cleanup.
- [x] Verify conflicting submissions, duplicate dispatch, stale-worker writes,
  and failures between persistence/publication operations.

Suggested commit: `feat: persist analysis jobs with idempotent queue delivery`.

The [storage walkthrough](serving-configuration.md#durable-jobs-and-private-storage)
explains the item keys, transaction boundaries, test command and deployment
requirements. This stage includes callable stream/schedule handlers, but no
AWS table, queue, IAM role, schedule or live fetch has been deployed.

## 3. API pipeline and entry point

- [x] Add FastAPI routes after the entities/repositories, then the API app and
  Mangum entry point. Keep external HTTP work and inference outside these routes.
- [x] Implement create/status/download/delete/current-model/liveness behavior,
  ownership checks, scope checks, limits and safe error envelopes.
- [x] Enforce canonical YouTube URL validation and a 2 KiB request-body cap.
- [x] Check the implemented OpenAPI schema against the contract and test
  unknown/foreign IDs, expiration, replay and same-key/different-body conflicts.

Suggested commit: `feat: expose authenticated analysis job endpoints`.

The [API walkthrough](serving-configuration.md#api-routes-and-local-running)
shows the local command, route flow, trust boundary and disabled live behavior.
The gateway's verified JWT context is consumed and scope-checked by the app;
Cognito resources and gateway scope configuration are still deployment work.

## 4. Fixture worker and shared prediction integration

- [x] Add the runtime native-thread override to PredictionService, preserving
  training settings and checksum/package/preprocessing validation.
- [x] Add a worker component, then its orchestration pipeline and Lambda entry
  point. Load one service per warm environment with DVC restoration disabled.
- [x] Use fixture video/comment responses to exercise pagination, duplicate IDs,
  empty/unknown/oversized text, partial samples and explicit result-size failures.
- [x] Join predictions by original index; reconcile every count and preserve
  confidence meaning, UTC dates and sample scope.
- [x] Test image/model release mismatch, checkpoint resume, attempt deadlines,
  lease expiry, successful result publication and crash recovery.

Suggested commit: `feat: process queued analyses with the shared sentiment model`.

The [worker walkthrough](serving-configuration.md#fixture-worker-and-shared-predictions)
explains page/batch checkpoints, fenced recovery, bundle verification and test
commands. The Lambda factory now accepts an explicit development fixture or
the approval-gated live provider added in step 6. Serving resources remain
undeployed. Model integration runs on small trained fixtures in Linux CI.

## 5. Authentication and quota controls

- [x] Configure Cognito public-client code flow and HTTP API route scopes in AWS
  CDK; prepare the extension's mandatory S256 PKCE helper.
- [x] Implement global/user admission counters, daily reservations, per-attempt
  spending, unused allowance release and Pacific-time quota rollover.
- [x] Validate the storage ledger's retries and daylight-saving boundaries.
- [x] Integrate spending with the worker: never issue a provider call without
  an allowance or reset a job's overall deadline.
- [x] Add structured metrics/logs without comments, names, tokens or keys.

Suggested commit: `feat: enforce user ownership and bounded YouTube quota use`.

The [auth and telemetry walkthrough](serving-configuration.md#authentication-quota-controls-and-monitoring)
describes the synthesized template, PKCE helper, metrics and checks. This step
implements and tests infrastructure code; it does not create a Cognito pool or
deployed login. Actual extension ID, API Lambda, notification routing and live
sign-in checks belong to the extension/deployment stages.

## 6. YouTube integration

- [x] Record the project's 10,000-unit daily allocation from the user-provided
  Google Cloud quota screenshot on 2026-10-14; keep an 8,000-unit working allowance.
- [ ] Verify analytics-use approval and record its reference.
- [x] Add the fixed-origin YouTube adapter, public-video metadata check, bounded
  latest top-level pagination, safe timeouts and classified provider errors.
- [x] Resume from checkpoints and enforce retry/reservation limits. No scraping,
  key rotation to evade quota, or hidden reply crawling.
- [ ] Run a small authorized live sample after fixture checks pass, retaining
  data only under the serving retention policy.

Suggested commit: `feat: fetch bounded public YouTube comment samples`.

This step's live test is gated on approval; the adapter can be written and tested
with fixtures while the external approval is pending. The
[YouTube walkthrough](serving-configuration.md#youtube-provider-and-live-readiness)
explains Secrets Manager wiring, durable retry delays, public checks on resume,
the shared project circuit and the remaining live-test prerequisites.

## 7. Extension connection

- [ ] Add user-initiated sign-in, state/PKCE validation, restricted session token
  storage and exact required host permissions.
- [ ] Connect submissions, progress polling, download renewal and owned deletion.
- [ ] Add queued/running/partial/empty/skipped/quota/busy/error/expired states.
- [ ] Keep demo mode labeled and available. Feed live charts/table/exports from
  one downloaded result; do not fetch on each filter change.
- [ ] Repeat desktop/mobile, keyboard, accessibility and CSV checks with API fixtures.

Suggested commit: `feat: connect the extension dashboard to analysis jobs`.

## 8. AWS deployment, load test and release

- [ ] Choose the monthly budget and configure cost/operational alarms.
- [ ] Add CDK environments, separate IAM roles, private serving prefixes,
  lifecycle/CORS rules, queues/DLQs, tables/indexes, triggers and cleanup schedule.
- [ ] Build pinned Linux worker images and run golden predictions; promote via
  immutable manifests/queues and GitHub OIDC, with rollback kept available.
- [ ] Check account Lambda concurrency and other service quotas before setting
  reserved concurrency. Keep all release mappings within the worker cap.
- [ ] Run the system design's 1,000-user fixture/load and fault-injection checks,
  then measure real fetch duration, memory, cost and cache hit ratio.
- [ ] Recalculate concurrency/admission settings from measurements; document
  results and unresolved limits before enabling live production analysis.

Suggested commit: `feat: deploy and validate the AWS analysis service`.

## Suggested source layout

```text
config/serving.yaml                 # New serving settings, separate from training
src/Sentiment_analysis/
  entity/serving.py                 # Typed settings and common validation types
  entity/analysis.py                # Request, status, result and queue entities
  entity/api.py                     # Principal, liveness and immutable release manifest
  config/serving.py                 # Validate and resolve serving settings
  config/worker.py                  # Verify packaged release/training metadata
  config/worker_runtime.py          # Lazy warm-worker model/client wiring
  api/                             # FastAPI schemas, routes, app and Lambda adapter
  repositories/                    # DynamoDB jobs, cache, quota and outbox
  integrations/                    # YouTube and S3 adapters
  services/prediction.py            # Existing shared inference; add thread override
  components/analysis.py           # Fetch, predict, assemble result
  components/job_delivery.py       # Send durable outbox messages
  pipeline/job_maintenance.py      # Recover leases/deadlines and clean expired data
  pipeline/api.py                  # HTTP use cases without fetching or inference
  pipeline/analysis.py             # Worker orchestration and terminal outcomes
  workers/                         # Analyze, dispatch and reconcile entry points
infra/                             # AWS CDK Python and release configuration
tests/serving/                     # Contracts, ownership, transactions, recovery
tests/load/                        # Fixture-based user/job load scenarios
```

The exact file names can follow repository conventions, but the order stays
settings -> entities -> configuration manager -> components -> pipelines ->
entry points. API, dispatcher and model worker use separate deployment packages;
the API never needs the model, notebooks, MLflow server, or raw dataset.
