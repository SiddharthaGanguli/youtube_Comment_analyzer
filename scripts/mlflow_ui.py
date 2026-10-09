"""Open the local tracking server with the same database and S3 credentials as training."""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.Sentiment_analysis.config.config import ConfigurationManager
from src.Sentiment_analysis.utils.aws_credentials import aws_credentials
from src.Sentiment_analysis.utils.experiment_tracking import load_mlflow


def main():
    settings = ConfigurationManager().get_mlflow_config()
    if settings is None:
        raise SystemExit("Enable mlflow in config/config.yaml first")
    load_mlflow()
    with aws_credentials(PROJECT_ROOT):
        subprocess.run([
            sys.executable, "-m", "mlflow", "server", "--backend-store-uri", settings.tracking_uri,
            "--default-artifact-root", settings.artifact_location, "--no-serve-artifacts",
            "--host", "127.0.0.1", "--port", "5000", "--workers", "1",
        ], cwd=PROJECT_ROOT, check=True)


if __name__ == "__main__":
    main()
