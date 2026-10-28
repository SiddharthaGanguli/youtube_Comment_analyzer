"""Build an unpacked Chrome extension from an explicit runtime-file allowlist.

Run ``python scripts/build_extension_bundle.py`` for a demo-only bundle, or pass
``--settings public-settings.json`` after the serving deployment is ready.
Settings contain public URLs and identifiers only; Node validates them with the
same JavaScript module used by the extension. Nothing is deployed by this tool.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension"
OUTPUT = ROOT / "artifacts" / "extension"
RUNTIME_FILES = (
    "index.html", "popup.html", "dashboard.html", "privacy.html", "terms.html",
    "styles.css", "home.css", "privacy.css",
    "home.mjs", "popup.mjs", "dashboard.mjs", "dashboard-app.mjs", "job-controller.mjs",
    "lib.mjs", "demo-data.mjs", "client-config.mjs", "auth-session.mjs",
    "auth-pkce.mjs", "analysis-client.mjs", "result-model.mjs",
    "assets/developed-with-youtube.png",
)
MARKER_FILE = ".comment-lens-bundle.json"
MARKER = {"builder": "comment-lens-extension", "schema_version": 1}
BUNDLE_FILES = frozenset(RUNTIME_FILES) | {"manifest.json", "client-settings.mjs", MARKER_FILE}
PUBLIC_FIELDS = frozenset({
    "enabled", "apiBaseUrl", "cognitoDomain", "clientId", "extensionId",
    "resultBaseUrl", "pollScheduleSeconds", "maxComments",
})
MAX_SETTINGS_BYTES = 16 * 1024

# JSON travels over stdin, never through shell source or command interpolation.
# Validation errors deliberately do not print settings, URLs, or token values.
VALIDATE_SCRIPT = """
import { pathToFileURL } from 'node:url';
process.stdin.setEncoding('utf8');
let input = '';
for await (const chunk of process.stdin) input += chunk;
try {
  const { validateClientConfig } = await import(pathToFileURL(process.argv[1]).href);
  const checked = validateClientConfig(JSON.parse(input));
  process.stdout.write(JSON.stringify(checked));
} catch {
  process.stderr.write('Invalid public extension settings\\n');
  process.exitCode = 1;
}
"""


def _linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _ordinary_path(path):
    """Reject links in ancestors too, before resolving a destination."""
    path = Path(path).absolute()
    if any(_linked(part) for part in (path, *path.parents)):
        raise ValueError("Bundle paths must not contain symlinks or junctions")
    return path.resolve()


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON fields are not allowed")
        result[key] = value
    return result


def _invalid_number(_value):
    raise ValueError("Settings must use finite JSON numbers")


def _read_json(data):
    try:
        return json.loads(data, object_pairs_hook=_json_object, parse_constant=_invalid_number)
    except (ValueError, UnicodeError, TypeError):
        raise ValueError("Invalid public extension settings JSON") from None


def _public_settings(settings):
    if settings is None:
        return {"enabled": False}
    with settings.open("rb") as handle:
        data = handle.read(MAX_SETTINGS_BYTES + 1)
    if len(data) > MAX_SETTINGS_BYTES:
        raise ValueError("Public extension settings are too large")
    raw = _read_json(data)
    if not isinstance(raw, dict) or set(raw) - PUBLIC_FIELDS or type(raw.get("enabled")) is not bool:
        raise ValueError("Only supported public extension settings are allowed; never include credentials")
    return raw


def _validate_settings(raw, source, node_executable=None):
    node = node_executable or shutil.which("node")
    if not node:
        raise ValueError("Node.js is required to validate extension settings")
    try:
        result = subprocess.run(
            [str(node), "--input-type=module", "--eval", VALIDATE_SCRIPT, "--", str(source / "client-config.mjs")],
            input=json.dumps(raw, allow_nan=False), capture_output=True, text=True,
            encoding="utf-8", timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ValueError("The public extension settings validator could not run") from None
    if result.returncode or len(result.stdout.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise ValueError("The extension connection settings are invalid")
    checked = _read_json(result.stdout)
    if (not isinstance(checked, dict) or set(checked) - PUBLIC_FIELDS
            or type(checked.get("enabled")) is not bool):
        raise ValueError("The extension settings validator returned invalid metadata")
    return checked


def _origin_pattern(value):
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.port or url.query or url.fragment or "*" in url.netloc):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Extension host permissions require exact HTTPS origins") from None
    return f"https://{url.hostname}/*"


def _manifest(settings):
    enabled = settings["enabled"]
    manifest = {
        "manifest_version": 3,
        "name": "Comment Lens",
        "version": "0.2.0",
        "description": (
            "Sign in to analyze public YouTube comment samples and explore sentiment in a full dashboard."
            if enabled else "Explore a sample dashboard for YouTube comment sentiment; live analysis is disabled."
        ),
        "permissions": ["activeTab", "identity", "storage"] if enabled else ["activeTab"],
        "action": {"default_title": "Open Comment Lens", "default_popup": "popup.html"},
        "content_security_policy": {"extension_pages": "script-src 'self'; object-src 'none';"},
    }
    if enabled:
        manifest["host_permissions"] = list(dict.fromkeys(
            _origin_pattern(settings[key]) for key in ("apiBaseUrl", "cognitoDomain", "resultBaseUrl")
        ))
        if len(manifest["host_permissions"]) != 3:
            raise ValueError("API, sign-in and result storage must have three distinct origins")
    return manifest


def _check_output(output):
    if not output.exists():
        return
    if not output.is_dir():
        raise ValueError("The extension output must be a directory")
    entries = list(output.iterdir())
    if not entries:
        return
    allowed_dirs = {parent.as_posix() for name in BUNDLE_FILES for parent in Path(name).parents if parent != Path(".")}
    pending = [output]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            if _linked(path):
                raise ValueError("The existing bundle contains a symlink or junction")
            relative = path.relative_to(output).as_posix()
            if path.is_dir():
                if relative not in allowed_dirs:
                    raise ValueError("The output contains unexpected directories; use a separate empty directory")
                pending.append(path)
            elif not path.is_file() or relative not in BUNDLE_FILES:
                raise ValueError("The output contains unexpected files; use a separate empty directory")
    marker = output / MARKER_FILE
    if not marker.is_file():
        raise ValueError("The existing output is not an owned extension bundle; use an empty directory")
    try:
        owned = _read_json(marker.read_bytes()) == MARKER
    except ValueError:
        owned = False
    if not owned:
        raise ValueError("The existing output is not an owned extension bundle; use an empty directory")


def _write_bytes(target, data):
    """Replace a known output file without truncating any external hard link."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".bundle-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_extension_bundle(source=SOURCE, output=OUTPUT, settings=None, node_executable=None):
    source, output = _ordinary_path(source), _ordinary_path(output)
    if source == output or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("The extension source and output must not overlap")
    if not source.is_dir():
        raise ValueError("The extension source must be an ordinary directory")
    settings_path = _ordinary_path(settings) if settings is not None else None
    if settings_path is not None and (settings_path == output or settings_path.is_relative_to(output)):
        raise ValueError("The public settings file must be outside the output directory")

    checked_files = []
    for name in RUNTIME_FILES:
        path = source / name
        resolved = _ordinary_path(path)
        if not resolved.is_file() or not resolved.is_relative_to(source):
            raise ValueError("An extension runtime file is missing or invalid")
        checked_files.append((name, path))
    _check_output(output)
    checked = _validate_settings(_public_settings(settings_path), source, node_executable)
    manifest = _manifest(checked)
    settings_module = (
        "// Generated public deployment metadata. Never put credentials in this file.\n"
        f"export const clientSettings = Object.freeze({json.dumps(checked, ensure_ascii=True, allow_nan=False)});\n"
    ).encode("utf-8")

    # All inputs and the existing output are checked before writing anything.
    output.mkdir(parents=True, exist_ok=True)
    _write_bytes(output / MARKER_FILE, (json.dumps(MARKER, indent=2) + "\n").encode("utf-8"))
    for name, path in checked_files:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes(target, path.read_bytes())
    _write_bytes(output / "client-settings.mjs", settings_module)
    _write_bytes(output / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, help="Public deployment JSON only; never credentials or tokens")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="Empty directory or an existing bundle created by this tool")
    args = parser.parse_args(argv)
    try:
        output = build_extension_bundle(output=args.output, settings=args.settings)
    except (ValueError, OSError) as error:
        print(f"Extension bundle was not built: {error}", file=sys.stderr)
        return 1
    print(f"Built extension in {output}")
    print("Load this directory with Chrome's Load unpacked command. No service was deployed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
