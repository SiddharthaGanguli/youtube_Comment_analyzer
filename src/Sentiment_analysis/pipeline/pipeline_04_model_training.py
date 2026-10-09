from src.Sentiment_analysis.components.model_training import ModelTraining
from src.Sentiment_analysis.config.config import ConfigurationManager


class ModelTrainingPipeline:
    def __init__(self, config_manager=None):
        self.config_manager = config_manager or ConfigurationManager()

    def run(self):
        return ModelTraining(self.config_manager.get_model_training_config()).run()
