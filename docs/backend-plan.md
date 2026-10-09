# Backend implementation plan

The [system design](system-design.md) sets the launch target and its limits.
This checklist turns that design into small reviewable steps. None of the
backend work below is complete yet. Finish and verify each step, then commit
and push it before starting the next, following the project's existing workflow.

Keep the offline training pipeline intact. Serving gets its own typed settings
so a change to polling or worker concurrency does not invalidate DVC training.
Use the [settings example](serving-settings.example.yaml) as the starting point,
and the [OpenAPI contract](backend-contract.yaml) as the interface to implement.

## 1. Serving configuration and entities

- [ ] Add serving YAML, typed entities, then a serving configuration manager.
- [ ] Validate cross-field rules: visibility >= 6 * timeout, lease > timeout,
  total mapping concurrency <= reserved concurrency, soft deadline < timeout,
  sample/page bounds, and freshness < retention.
- [ ] Require model/release fingerprints, public-only scope, exact allowed
  origins, secret references and no embedded secret values.
- [ ] Keep live YouTube analysis disabled without recorded analytics-use approval.
- [ ] Define typed request/status/result/error entities matching the contract.

Suggested commit: `feat: add typed serving configuration and API entities`.

## 2. Durable job and storage components

- [ ] Add DynamoDB repositories for owned requests, conditional cache claims,
  idempotency, fenced work leases, quota counters, active slots and the outbox.
- [ ] Add private S3 checkpoint/result components with immutable attempt keys,
  hashes, byte ceilings and expiry-aware download signing.
- [ ] Implement transactional admission/terminal release; reclaim stale slots
  without depending on TTL deletion.
- [ ] Add dispatcher and scheduled reconciliation for pending outbox, expired
  leases, deadlines and data cleanup.
- [ ] Verify conflicting submissions, duplicate dispatch, stale-worker writes,
  and failures between persistence/publication operations.

Suggested commit: `feat: persist analysis jobs with idempotent queue delivery`.

## 3. API pipeline and entry point

- [ ] Add FastAPI routes after the entities/repositories, then the API app and
  Mangum entry point. Keep external HTTP work and inference outside these routes.
- [ ] Implement create/status/download/delete/current-model/liveness behavior,
  ownership checks, scope checks, limits and safe error envelopes.
- [ ] Enforce canonical YouTube URL validation and a 2 KiB request-body cap.
- [ ] Check the implemented OpenAPI schema against the proposed contract and test
  unknown/foreign IDs, expiration, replay and same-key/different-body conflicts.

Suggested commit: `feat: expose authenticated analysis job endpoints`.

## 4. Fixture worker and shared prediction integration

- [ ] Add the runtime native-thread override to PredictionService, preserving
  training settings and checksum/package/preprocessing validation.
- [ ] Add a worker component, then its orchestration pipeline and Lambda entry
  point. Load one service per warm environment with DVC restoration disabled.
- [ ] Use fixture video/comment responses to exercise pagination, duplicate IDs,
  empty/unknown/oversized text, partial samples and explicit result-size failures.
- [ ] Join predictions by original index; reconcile every count and preserve
  confidence meaning, UTC dates and sample scope.
- [ ] Test image/model release mismatch, checkpoint resume, attempt deadlines,
  lease expiry, successful result publication and crash recovery.

Suggested commit: `feat: process queued analyses with the shared sentiment model`.

## 5. Authentication and quota controls

- [ ] Configure Cognito public-client PKCE and HTTP API route scopes in AWS CDK.
- [ ] Implement global/user admission counters, daily reservations, per-attempt
  spending, unused allowance release and Pacific-time quota rollover.
- [ ] Validate retries and daylight-saving boundaries; never issue a provider
  call without an allowance or reset a job's overall deadline.
- [ ] Add structured metrics/logs without comments, names, tokens or keys.

Suggested commit: `feat: enforce user ownership and bounded YouTube quota use`.

## 6. YouTube integration

- [ ] Verify analytics-use approval and the project's actual daily allocation.
- [ ] Add the fixed-origin YouTube adapter, public-video metadata check, bounded
  latest top-level pagination, safe timeouts and classified provider errors.
- [ ] Resume from checkpoints and enforce retry/reservation limits. No scraping,
  key rotation to evade quota, or hidden reply crawling.
- [ ] Run a small authorized live sample after fixture checks pass, retaining
  data only under the serving retention policy.

Suggested commit: `feat: fetch bounded public YouTube comment samples`.

This step's live test is gated on approval; the adapter can be written and tested
with fixtures while the external approval is pending.

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
  entity/serving.py                 # Typed settings and domain/job entities
  config/serving.py                 # Validate and resolve serving settings
  api/                             # FastAPI schemas, routes, app and Lambda adapter
  repositories/                    # DynamoDB jobs, cache, quota and outbox
  integrations/                    # YouTube and S3 adapters
  services/prediction.py            # Existing shared inference; add thread override
  components/analysis.py           # Fetch, predict, assemble result
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
