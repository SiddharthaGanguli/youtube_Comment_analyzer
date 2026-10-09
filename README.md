# YouTube Comment Analyzer

YouTube comment sentiment analysis with reproducible data and model pipelines.

## Run the pipeline

Each stage follows `config.yaml` → typed configuration in `entity.py` →
`ConfigurationManager` → component → pipeline → `main.py`.

Run individual stages from the repository root:

```powershell
.\.venv\Scripts\python.exe main.py --stage ingestion
.\.venv\Scripts\python.exe main.py --stage validation
.\.venv\Scripts\python.exe main.py --stage preprocessing
.\.venv\Scripts\python.exe main.py --stage training
.\.venv\Scripts\python.exe main.py --stage evaluation
```

Ingestion verifies the configured file size and SHA-256. If the CSV is missing,
it restores the DVC-tracked version using the existing S3 remote settings.
Its report is saved under `artifacts/data_ingestion/`; a corrupt local file fails
verification rather than being overwritten. Run the stage checks with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Validation checks the source checksum, required columns, allowed numeric labels,
and agreement with the text labels. Structural failures stop the stage and write
a failed report. Empty comments/titles and conflicting title/comment label groups
are listed in `artifacts/data_validation/quarantine_rows.csv` for preprocessing
to exclude. Exact duplicate rows are counted for the next stage to remove.

Preprocessing removes quarantined rows and exact duplicates, decodes HTML,
normalizes Unicode/whitespace, and preserves punctuation, emoji, and negation.
All languages are retained. Title groups are split approximately 80/10/10 using
seed 42. Repeated comment text is removed from training/validation when it occurs
in a higher-priority holdout split; the test set stays fixed. The stage checks
that title, comment, and source-row overlap are zero and records each split's
class counts and checksum under `artifacts/data_preprocessing/`.

Install the pipeline dependencies before running preprocessing or training:

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-pipeline.txt
```

Training fits a single scikit-learn Pipeline containing TF-IDF and logistic
regression. Vocabulary, IDF, and classifier weights are learned from training
rows only. The validation split is scored after fitting; training never reads
the test CSV. Hyperparameters live in `config/params.yaml`. The stage requires
convergence and saves the fitted pipeline, label mapping, source checksums, and
package versions in `artifacts/model_training/sentiment_model.joblib`, with a
training report and a majority-class baseline alongside it.

Evaluation uses the test split after training. It verifies the model and split
checksums and checks all splits again for overlap. Accuracy, balanced accuracy,
macro/weighted F1, log loss, and the training-majority baseline are written to
`reports/evaluation_metrics.json`. The per-class report, confusion matrix, and a
100-row sample of misclassified comments live under `artifacts/model_evaluation/`.
These scores measure agreement with the supplied labels; manual label review is
still pending.

The first full run used 793,424 training rows, 97,335 validation rows, and 103,653
test rows, with zero title/comment/source-row overlap across splits.

| Metric | Validation | Test |
| --- | --- | --- |
| Accuracy | 70.83% | 71.59% |
| Macro F1 | 70.89% | 71.69% |
| Majority-class accuracy | 34.94% | 34.01% |

Run all five stages in sequence:

```powershell
.\.venv\Scripts\python.exe main.py --stage all
```

For DVC to rerun only stages whose dependencies changed, activate the environment
and reproduce the pipeline:

```powershell
.\.venv\Scripts\Activate.ps1
python -m dvc repro
python -m dvc metrics show
```

`dvc.yaml` defines the dependencies and artifacts. Commit it together with
`dvc.lock` and the metrics JSON. Datasets, split CSVs, model binaries, and logs are
excluded from Git. The new pipeline artifacts are cached locally by DVC;
`python -m dvc push` can publish those artifacts to the configured S3 remote.

GitHub Actions runs the stage checks on small fixtures without downloading the
full dataset or using AWS credentials. They cover corrupted inputs, inconsistent
labels, quarantine handling, split overlap, held-out vocabulary, persisted models,
and test evaluation.

The baseline uses [TF-IDF](https://scikit-learn.org/stable/modules/generated/sklearn.feature_extraction.text.TfidfVectorizer.html)
and [logistic regression](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html)
inside a Pipeline, following scikit-learn's guidance on [preventing data leakage](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage).

## Python environment

The project uses Python 3.12 in `.venv/`. VS Code's project settings point to
that environment. To recreate it on a new checkout, run these commands from
the repository root:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dvc.txt -r requirements-eda.txt -r requirements-pipeline.txt
```

To activate it in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

In the notebook, choose **Select Kernel > Select Another Kernel > Python
Environments**, then select the project's `.venv` interpreter. If VS Code already
has a different interpreter selected, run **Python: Select Interpreter** and
select `.venv/Scripts/python.exe`; the default setting does not replace an
existing selection. The environment itself is ignored by Git.

## Dataset and configuration

The raw dataset is [Arshia82sbn/youtube-sentiment-dataset](https://huggingface.co/datasets/Arshia82sbn/youtube-sentiment-dataset).

| File | Purpose |
| --- | --- |
| `data/raw/youtube-comments-sentiment.csv` | Full downloaded dataset; excluded from Git |
| `data/raw/youtube-comments-sentiment.csv.dvc` | Dataset hash, size, and path; version with Git |
| `config/config.yaml` | Source revision, SHA-256 checksum, paths, columns, and label mapping |
| `.dvc/config` | Shared DVC settings; currently uses a local copy cache |
| `.dvc/config.local` | Machine-specific settings; excluded from Git |
| `requirements-dvc.txt` | Pinned DVC version with Amazon S3 support |

DVC caches the dataset under `.dvc/cache`, which is excluded from Git. Folder
markers (`.gitkeep`) and `.dvc` metadata remain eligible for Git tracking.
The source SHA-256 checksum in `config/config.yaml` verifies the download; the
MD5 hash in the `.dvc` file identifies the object in DVC's cache.

## Explore the data

Start with [01_youtube_comments_eda.ipynb](notebooks/01_youtube_comments_eda.ipynb).
The notebook explains each check, shows the results, and includes a few exercises
to help you understand the choices we will make in the pipeline.

Install the notebook dependencies into the project environment:

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-eda.txt
```

Open the notebook in VS Code, select `.venv/Scripts/python.exe` as the notebook
kernel, and run the cells from top to bottom. The full CSV is used for quality
checks; sampled plots and word counts are labeled with their sample sizes.
If the CSV is missing, restore it with the `dvc pull` command below.

The notebook writes two local files under `data/processed/eda/`:

| File | Purpose |
| --- | --- |
| `data_audit.json` | Counts, source checksum, package versions, and review progress |
| `label_review_sample.csv` | 200 comments with blank fields for your labels and notes |

Both files are ignored by Git. Rerunning the notebook preserves an existing
review worksheet and checks that its source rows still match the raw CSV.
The raw dataset is never modified.

The first audit found 1,032,225 rows, 12,241 exact duplicate rows, and 705
title/comment pairs with conflicting labels. The supplied cleaned text is empty
for 31,064 nonempty raw comments. We will review these findings and the manual
sample before implementing ingestion, validation, or preprocessing.

## DVC storage

The default remote is `storage`, at
`s3://yt-comment-analyzer-main/datasets/youtube-comments` in `us-east-1`.
Local tracking and cache operations work. Check cloud synchronization with
`dvc status --cloud`; the IAM access described below is required for the remote.
The full raw dataset has been uploaded with DVC. DVC stores the remote
object by its hash under `files/md5/`; a targeted `dvc pull` restores it to the CSV path
recorded in `data/raw/youtube-comments-sentiment.csv.dvc`.

## Local DVC commands (PowerShell)

Run these commands from the repository root. If `.venv` does not exist, create it
and install the DVC dependencies:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dvc.txt
```

Check the tracked dataset:

```powershell
.\.venv\Scripts\python.exe -m dvc status
```

After deliberately updating the raw dataset, update its source metadata in
`config/config.yaml` and refresh the DVC pointer:

```powershell
.\.venv\Scripts\python.exe -m dvc add data/raw/youtube-comments-sentiment.csv
```

Git stores the configuration and DVC pointer. To prepare those files for a commit:

```powershell
git add .gitignore .dvc/.gitignore .dvc/config .dvcignore
git add data/raw/youtube-comments-sentiment.csv.dvc config/config.yaml config/aws-dvc-policy.json
git add requirements.txt requirements-dvc.txt requirements-eda.txt README.md
git add notebooks/01_youtube_comments_eda.ipynb
```

## Amazon S3 access

Shared remote settings are in `.dvc/config`. Credentials are currently stored
only in the Git-ignored `.dvc/config.local`. Replace any key shared in chat with
new credentials entered locally, or switch to an AWS profile or IAM role.
Never put credential values in `.dvc/config`, application configuration, or Git.

Attach `config/aws-dvc-policy.json` to the IAM identity used by DVC, or grant
equivalent permissions through its group or role. This policy allows listing
the selected bucket and reading/writing only the `datasets/youtube-comments/`
prefix, including multipart upload handling. It does not grant object deletion.
If access is still denied, check bucket policies, permission boundaries, and
organization policies for additional restrictions. Cross-account access may
also require a bucket policy from the bucket owner.

After access is granted, check the remote and upload:

```powershell
.\.venv\Scripts\python.exe -m dvc status --cloud
.\.venv\Scripts\python.exe -m dvc push
# On another checkout, restore the raw source before reproducing the pipeline:
.\.venv\Scripts\python.exe -m dvc pull data/raw/youtube-comments-sentiment.csv.dvc
```

These examples follow the [DVC Amazon S3 documentation](https://dvc.org/doc/user-guide/data-management/remote-storage/amazon-s3).
The IAM policy uses [AWS S3 identity-policy resource scopes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/example-policies-s3.html)
and [multipart upload permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html).
