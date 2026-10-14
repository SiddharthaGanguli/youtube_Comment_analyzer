"""Read the auth template settings without credentials or a deployed pool."""

from pathlib import Path

import yaml

from src.Sentiment_analysis.config.serving import PROJECT_ROOT, UniqueKeyLoader
from src.Sentiment_analysis.entity.auth import AuthInfrastructureSettings


class AuthConfigurationManager:
    def __init__(self, path=None, project_root=PROJECT_ROOT):
        self.project_root = Path(project_root).resolve()
        path = Path(path) if path else Path("config/auth-infrastructure.yaml")
        self.path = path if path.is_absolute() else self.project_root / path

    def get_auth_config(self):
        if self.path.stat().st_size > 65536:
            raise ValueError("Auth settings exceed the configuration limit")
        try:
            data = yaml.load(self.path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        except yaml.YAMLError:
            raise ValueError("Invalid auth configuration") from None
        return AuthInfrastructureSettings.model_validate(data)
