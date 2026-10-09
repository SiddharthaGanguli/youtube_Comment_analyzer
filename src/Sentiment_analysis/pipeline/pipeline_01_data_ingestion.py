from src.Sentiment_analysis.components.data_ingestion import DataIngestion
from src.Sentiment_analysis.config.config import ConfigurationManager


class DataIngestionPipeline:
    def __init__(self, config_manager=None):
        self.config_manager = config_manager or ConfigurationManager()

    def run(self):
        config = self.config_manager.get_data_ingestion_config()
        return DataIngestion(config).run()
