import pandas as pd

from src.Sentiment_analysis.entity.entity import ModelTrainingConfig
from src.Sentiment_analysis.utils.common import sha256_file, write_json


def training_fixture(root):
    paths = {}
    metadata = {}
    for offset, name in enumerate(["train", "validation", "test"]):
        rows = []
        for label, word in enumerate(["terrible", "ordinary", "wonderful"]):
            for number in range(6):
                text = f"{word} not mood 😊 {name}onlyquokka item{number}"
                rows.append([offset * 100 + label * 10 + number, f"{name} video {label}", text, label])
        frame = pd.DataFrame(rows, columns=["source_row", "VideoTitle", "CommentText", "Sentiment_label"])
        paths[name] = root / f"{name}.csv"
        frame.to_csv(paths[name], index=False)
        metadata[name] = {"rows": len(frame), "sha256": sha256_file(paths[name])}
    report = root / "preprocessing.json"
    write_json(report, {"passed": True, "splits": metadata,
                        "parameters": {"unicode_normalization": "NFKC", "language_policy": "keep_all"}})
    config = ModelTrainingConfig(
        paths["train"], paths["validation"], report, root / "model.joblib", root / "training.json",
        "utf-8", "CommentText", "VideoTitle", "Sentiment_label", {0: "Negative", 1: "Neutral", 2: "Positive"},
        {"random_state": 42, "thread_limit": 1,
         "tfidf": {"max_features": 1000, "ngram_range": [1, 2], "min_df": 1, "max_df": 1.0,
                   "sublinear_tf": True, "token_pattern": r"(?u)\b\w+(?:['’]\w+)*\b|[^\w\s]"},
         "logistic_regression": {"C": 1.0, "solver": "lbfgs", "max_iter": 200, "tol": .001}},
    )
    return config, paths
