"""Load and validate serving settings without importing the ML pipeline."""

import argparse
from pathlib import Path, PureWindowsPath

import yaml

from src.Sentiment_analysis.entity.serving import ServingSettings

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class UniqueKeyLoader(yaml.SafeLoader):
    """Fail on duplicate YAML keys instead of silently keeping the last value."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("Serving YAML keys must be strings")
            if key in mapping:
                raise ValueError(f"Duplicate serving YAML key: {key}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class ServingConfigurationManager:
    """YAML -> immutable typed settings. No model load, DVC pull or AWS call."""

    def __init__(self, config_path=None, project_root=PROJECT_ROOT):
        self.project_root = Path(project_root).resolve()
        path = Path(config_path) if config_path is not None else Path("config/serving.yaml")
        self.config_path = path if path.is_absolute() else self.project_root / path

    def _artifact_path(self, value):
        if (not isinstance(value, str) or not value.strip() or Path(value).is_absolute()
                or PureWindowsPath(value).drive):
            raise ValueError("Artifact paths must be nonempty paths relative to the project")
        path = (self.project_root / value.replace("\\", "/")).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("Serving artifact path escapes the project root")
        return path

    def get_serving_config(self) -> ServingSettings:
        if self.config_path.stat().st_size > 65536:
            raise ValueError("Serving YAML exceeds the 64 KiB configuration limit")
        try:
            data = yaml.load(self.config_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        except yaml.YAMLError:
            raise ValueError("Serving YAML is invalid or uses unsupported tags") from None
        if not isinstance(data, dict) or not isinstance(data.get("model"), dict):
            raise ValueError("Serving YAML needs a mapping with a model section")
        model = dict(data["model"])
        for key in ("model_file", "training_report"):
            model[key] = self._artifact_path(model.get(key))
        return ServingSettings.model_validate({**data, "model": model})


def main():
    parser = argparse.ArgumentParser(description="Validate backend settings without calling AWS or YouTube")
    parser.add_argument("--config", type=Path, default=Path("config/serving.yaml"))
    parser.add_argument("--production", action="store_true", help="Also require an explicit deployment-ready production configuration")
    args = parser.parse_args()
    try:
        config = ServingConfigurationManager(args.config).get_serving_config()
        if args.production:
            config.validate_for_deployment()
    except (OSError, ValueError) as error:
        parser.exit(2, f"Serving configuration invalid: {error}\n")
    print(f"Serving configuration valid ({config.environment})")
    print(f"Live YouTube analysis: {'enabled' if config.youtube.live_enabled else 'disabled'}")
    print(f"Serving release: {'assigned' if config.model.release_id else 'not assigned yet'}")
    quota_status = "project allocation marked verified" if config.youtube.project_quota_verified else "configured, not externally verified"
    print(f"Working daily quota: {config.youtube.working_daily_quota_units} units ({quota_status})")


if __name__ == "__main__":
    main()
