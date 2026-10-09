from src.Sentiment_analysis.components.data_preprocessing import DataPreprocessing
from src.Sentiment_analysis.config.config import ConfigurationManager


class DataPreprocessingPipeline:
    def __init__(self, config_manager=None):
        self.config_manager = config_manager or ConfigurationManager()

    def run(self):
        return DataPreprocessing(self.config_manager.get_data_preprocessing_config()).run()
