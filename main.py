import argparse
import os
from pathlib import Path

from logging_.logger_ import Logger
from src.Sentiment_analysis.config.config import ConfigurationManager
from src.Sentiment_analysis.pipeline.pipeline_01_data_ingestion import DataIngestionPipeline
from src.Sentiment_analysis.pipeline.pipeline_02_data_validation import DataValidationPipeline
from src.Sentiment_analysis.pipeline.pipeline_03_data_preprocessing import DataPreprocessingPipeline
from src.Sentiment_analysis.pipeline.pipeline_04_model_training import ModelTrainingPipeline
from src.Sentiment_analysis.pipeline.pipeline_05_model_evaluation import ModelEvaluationPipeline
from src.Sentiment_analysis.pipeline.pipeline_06_model_prediction import ModelPredictionPipeline


STAGES = {"ingestion": DataIngestionPipeline, "validation": DataValidationPipeline,
          "preprocessing": DataPreprocessingPipeline, "training": ModelTrainingPipeline,
          "evaluation": ModelEvaluationPipeline}


def main():
    parser = argparse.ArgumentParser(description="Run the YouTube sentiment pipeline")
    parser.add_argument("--stage", choices=["all", *STAGES, "prediction"], default="all")
    parser.add_argument("--config", type=Path, help="Configuration YAML (defaults to config/config.yaml)")
    parser.add_argument("--params", type=Path, help="Parameters YAML (defaults to config/params.yaml)")
    parser.add_argument("--input", type=Path, help='Prediction input JSON containing a "comments" list')
    parser.add_argument("--output", type=Path, help="Prediction output JSON (optional)")
    args = parser.parse_args()
    if args.stage == "prediction" and args.input is None:
        parser.error("--stage prediction requires --input")
    if args.stage != "prediction" and (args.input is not None or args.output is not None):
        parser.error("--input and --output are only used by --stage prediction")
    # Resolve supplied paths before changing the working directory.
    config_path = args.config.resolve() if args.config else None
    params_path = args.params.resolve() if args.params else None
    input_path = args.input.resolve() if args.input else None
    output_path = args.output.resolve() if args.output else None
    os.chdir(Path(__file__).resolve().parent)
    manager = ConfigurationManager(config_path, params_path)
    logger = Logger("pipeline.log").get_logger()
    if args.stage == "prediction":
        result = ModelPredictionPipeline(manager).run(input_path, output_path)
        logger.info("Prediction complete: %s", result["summary"])
        return
    selected = STAGES if args.stage == "all" else {args.stage: STAGES[args.stage]}
    for name, pipeline in selected.items():
        logger.info("Starting %s", name)
        pipeline(manager).run()
        logger.info("Completed %s", name)


if __name__ == "__main__":
    main()
