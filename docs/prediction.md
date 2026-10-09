# Predicting new comments

`PredictionService` is the shared Python service for the future backend. It loads
one trained model and keeps it in memory. Each request supplies raw comment text.
It never fits TF-IDF, changes the vocabulary, or trains a classifier.

From the repository root, try the saved baseline:

```powershell
.\.venv\Scripts\python.exe main.py --stage prediction --input examples/comments.json
```

The result is written to `artifacts/model_prediction/predictions.json`. Pass
`--output` to choose another JSON file. Supplied paths are resolved from your
current directory, so the command also works from another directory when you
give the full path to `main.py`. Prediction is separate from `--stage all`, which
continues to run the five dataset/training/evaluation stages.

To reuse the service in Python:

```python
from src.Sentiment_analysis.config.config import ConfigurationManager
from src.Sentiment_analysis.services.prediction import PredictionService

service = PredictionService(ConfigurationManager().get_model_prediction_config())
result = service.predict(["Loved this explanation!", "This was not helpful.", None])
print(result["summary"])
# Reuse service for the next request instead of loading another copy of the model.
```

The CLI input is `{"comments": ["text", null]}`. The service accepts a list of
strings or `None`. Numbers, dictionaries, a bare string, and oversized requests
are rejected. Batch size, request limit, and the review threshold are in
`config/params.yaml` under `model_prediction`.

## Why preprocessing is saved with the model

Training and prediction both use `utils/text.py`. HTML entities are decoded once,
Unicode is normalized, and repeated whitespace is collapsed. Negation, emoji,
punctuation, and non-English words are kept. Lowercasing and tokenization remain
inside the fitted TF-IDF vectorizer.

The model bundle and training report contain the exact text recipe used to
prepare the training data. Prediction reads that recipe rather than today's
preprocessing YAML. A model without the recipe must be retrained. In particular,
call `predict` with raw text: decoding HTML twice can change double-encoded text.
The sklearn model logged in MLflow expects already normalized text; its metadata
records the recipe. Use this service with the DVC model bundle for raw comments.

Before loading the bundle, the service verifies its SHA-256 against the successful
training report and checks the installed package versions. It also verifies the
bundle's recipe, preprocessing fingerprint, text column, label mapping, and
classifier classes against the report/configuration. Missing artifacts can be
restored with DVC when `restore_with_dvc` is enabled. Changed artifacts fail
verification and require an intentional repair or retraining.

## Reading the response

`comments` keeps one row per input, in the same order. Each row has its original
`text`, `normalized_text`, and `index`, so a backend can join predictions to the
original comment IDs, authors, likes, and timestamps without losing skipped rows.

For an analyzed comment, `sentiment_label` is 0, 1, or 2; `sentiment` is Negative,
Neutral, or Positive. `class_probabilities` gives all three class scores and
`confidence` is their maximum. `needs_review` is true below the configured
threshold, currently 0.6. These scores have not been calibrated as a probability
that the prediction is correct. A confident prediction may still be wrong.

Empty comments receive `skipped_reason: "empty_text"`. A nonempty comment whose
TF-IDF vector has no known features receives `"no_known_features"`, instead of
an intercept-only prediction. Both have null labels and confidence. This gives
visibility into unfamiliar text and languages. Having some known features does
not establish that a language or comment is well supported.

`summary` reports received/analyzed/skipped counts, coverage, sentiment counts,
skip reasons, and the number needing review. Sentiment counts use analyzed
comments only. `model` identifies the model hash, MLflow run, recipe, and threshold.
The service supplies no video metadata or per-video accuracy estimate.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Prediction checks cover matching training normalization, decoding HTML once,
Unicode/emoji/negation, class-score agreement with the fitted pipeline, batching,
input order, missing and unfamiliar text, bad requests, model integrity, saved
recipe enforcement, DVC restoration, and CLI use from another directory.

The HTTP API, YouTube comment retrieval, extension connection, and AWS deployment
are the next application steps. This service does not perform them.
