# The first backend step: settings and entities

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

From the repository root, install the small settings/schema dependency set:

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

## Checks and the next stage

The serving tests use synthetic metadata and temporary YAML files; no dataset,
model, credentials or AWS service is required. In the project's test environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests/serving -t . -v
```

The tests cover configuration relationships, incomplete production settings,
secret/region/origin/path guards, YAML ambiguity, canonical URLs, timestamp
handling, invalid predictions and schema compatibility. The full repository
suite also runs them through the existing CI workflow.

Next we will build the DynamoDB job/cache/outbox repositories and private S3
checkpoint/result components. These entities are inputs to that stage; they do
not yet persist jobs, publish queue messages, verify JWTs or fetch comments.
