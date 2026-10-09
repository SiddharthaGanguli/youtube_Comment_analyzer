from src.Sentiment_analysis.components.data_validation import DataValidation
from src.Sentiment_analysis.config.config import ConfigurationManager


class DataValidationPipeline:
    def __init__(self, config_manager=None):
        self.config_manager = config_manager or ConfigurationManager()

    def run(self):
        return DataValidation(self.config_manager.get_data_validation_config()).run()
