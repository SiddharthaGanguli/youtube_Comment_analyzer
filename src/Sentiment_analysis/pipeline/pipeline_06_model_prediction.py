import json

from src.Sentiment_analysis.config.config import ConfigurationManager
from src.Sentiment_analysis.services.prediction import PredictionService
from src.Sentiment_analysis.utils.common import write_json


class ModelPredictionPipeline:
    def __init__(self, config_manager=None):
        self.config_manager = config_manager or ConfigurationManager()

    def run(self, input_file, output_file=None):
        config = self.config_manager.get_model_prediction_config()
        payload = json.loads(input_file.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or "comments" not in payload:
            raise ValueError('Input JSON must contain a "comments" list')
        result = PredictionService(config).predict(payload["comments"])
        write_json(output_file or config.report_file, result)
        return result
