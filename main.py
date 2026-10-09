import argparse
import os
from pathlib import Path

from logging_.logger_ import Logger
from src.Sentiment_analysis.config.config import ConfigurationManager
from src.Sentiment_analysis.pipeline.pipeline_01_data_ingestion import DataIngestionPipeline
from src.Sentiment_analysis.pipeline.pipeline_02_data_validation import DataValidationPipeline
from src.Sentiment_analysis.pipeline.pipeline_03_data_preprocessing import DataPreprocessingPipeline
from src.Sentiment_analysis.pipeline.pipeline_04_model_training import ModelTrainingPipeline


STAGES = {"ingestion": DataIngestionPipeline, "validation": DataValidationPipeline,
          "preprocessing": DataPreprocessingPipeline, "training": ModelTrainingPipeline}


def main():
    parser = argparse.ArgumentParser(description="Run the YouTube sentiment pipeline")
    parser.add_argument("--stage", choices=["all", *STAGES], default="all")
    parser.add_argument("--config", type=Path, help="Configuration YAML (defaults to config/config.yaml)")
    parser.add_argument("--params", type=Path, help="Parameters YAML (defaults to config/params.yaml)")
    args = parser.parse_args()
    # Resolve supplied paths before changing the working directory.
    config_path = args.config.resolve() if args.config else None
    params_path = args.params.resolve() if args.params else None
    os.chdir(Path(__file__).resolve().parent)
    manager = ConfigurationManager(config_path, params_path)
    logger = Logger("pipeline.log").get_logger()
    selected = STAGES if args.stage == "all" else {args.stage: STAGES[args.stage]}
    for name, pipeline in selected.items():
        logger.info("Starting %s", name)
        pipeline(manager).run()
        logger.info("Completed %s", name)


if __name__ == "__main__":
    main()
