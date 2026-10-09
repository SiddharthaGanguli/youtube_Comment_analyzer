"""Use normal AWS credentials, with a fallback to this machine's private DVC settings."""

import configparser
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def aws_credentials(project_root):
    # Profiles, environment credentials and deployed IAM roles take priority.
    explicit = any(os.environ.get(name) for name in (
        "AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_SHARED_CREDENTIALS_FILE",
    ))
    shared_file = Path.home() / ".aws/credentials"
    fallback = Path(project_root) / ".dvc/config.local"
    changes = {}
    if not explicit and not shared_file.exists() and fallback.exists():
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(fallback, encoding="utf-8")
        # DVC quotes the remote section name in its INI file.
        section = next((name for name in parser.sections() if name.strip("'") == 'remote "storage"'), None)
        if section:
            for source, target in (
                ("access_key_id", "AWS_ACCESS_KEY_ID"),
                ("secret_access_key", "AWS_SECRET_ACCESS_KEY"),
                ("session_token", "AWS_SESSION_TOKEN"),
            ):
                value = parser.get(section, source, fallback=None)
                if value:
                    changes[target] = value
    previous = {name: os.environ.get(name) for name in changes}
    try:
        os.environ.update(changes)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
