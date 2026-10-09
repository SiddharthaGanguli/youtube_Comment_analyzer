from src.Sentiment_analysis.components.model_evaluation import ModelEvaluation
from src.Sentiment_analysis.config.config import ConfigurationManager


class ModelEvaluationPipeline:
    def __init__(self, config_manager=None):
        self.config_manager = config_manager or ConfigurationManager()

    def run(self):
        return ModelEvaluation(self.config_manager.get_model_evaluation_config()).run()
