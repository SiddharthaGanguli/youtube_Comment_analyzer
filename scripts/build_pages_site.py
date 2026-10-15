"""Stage an allowlisted website; default to a demo with no service connection.

Use --settings public-web-settings.json only after deployment outputs exist.
The generated module contains public URLs/IDs, never credentials or API keys.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension"
OUTPUT = ROOT / "_site"
WEB_FILES = (
    "index.html", "dashboard.html", "privacy.html", "terms.html",
    "styles.css", "home.css", "privacy.css",
    "dashboard.mjs", "dashboard-app.mjs", "demo-data.mjs", "lib.mjs",
    "assets/developed-with-youtube.png",
)
CONNECTION_FILES = (
    "web-client-config.mjs", "web-auth-session.mjs", "auth-pkce.mjs",
    "analysis-client.mjs", "client-config.mjs", "job-controller.mjs", "result-model.mjs",
)
GENERATED_SETTINGS = "web-client-settings.mjs"
PUBLIC_FIELDS = frozenset({"enabled", "apiBaseUrl", "cognitoDomain", "clientId", "redirectUri", "resultBaseUrl",
                           "pollScheduleSeconds", "maxComments"})
MAX_SETTINGS_BYTES = 16 * 1024
VALIDATE_SCRIPT = """
import { pathToFileURL } from 'node:url';
process.stdin.setEncoding('utf8');
let input = '';
for await (const chunk of process.stdin) input += chunk;
try {
  const {validateWebClientConfig} = await import(pathToFileURL(process.argv[1]).href);
  process.stdout.write(JSON.stringify(validateWebClientConfig(JSON.parse(input))));
} catch {
  process.stderr.write('Invalid public website settings\\n');
  process.exitCode = 1;
}
"""


def linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def ordinary(path):
    path = Path(path).absolute()
    if any(linked(part) for part in (path, *path.parents)):
        raise ValueError("Website paths must not contain symlinks or junctions")
    return path.resolve()


def unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate public settings fields are not allowed")
        data[key] = value
    return data


def invalid_number(_value):
    raise ValueError("Public settings require finite JSON values")


def public_settings(settings, source, node_executable=None):
    if settings is None:
        return {"enabled": False}
    try:
        with settings.open("rb") as handle:
            data = handle.read(MAX_SETTINGS_BYTES + 1)
        if len(data) > MAX_SETTINGS_BYTES:
            raise ValueError("Oversized public settings")
        raw = json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_number)
        if (not isinstance(raw, dict) or set(raw) - PUBLIC_FIELDS or type(raw.get("enabled")) is not bool):
            raise ValueError("Unsupported settings fields")
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError("Provide valid public website settings JSON without credentials") from None
    node = node_executable or shutil.which("node")
    if not node:
        raise ValueError("Node.js is required to validate public website settings")
    validator = ordinary(source / "web-client-config.mjs")
    if not validator.is_file() or not validator.is_relative_to(source):
        raise ValueError("The website settings validator is missing or invalid")
    ordinary(source / "client-config.mjs")
    try:
        result = subprocess.run([str(node), "--input-type=module", "--eval", VALIDATE_SCRIPT, "--", str(validator)],
                                input=json.dumps(raw, allow_nan=False), capture_output=True, text=True,
                                encoding="utf-8", timeout=10, check=False)
        if result.returncode or len(result.stdout.encode("utf-8")) > MAX_SETTINGS_BYTES:
            raise ValueError("Validator failed")
        checked = json.loads(result.stdout, object_pairs_hook=unique_object, parse_constant=invalid_number)
        if (not isinstance(checked, dict) or set(checked) - PUBLIC_FIELDS or type(checked.get("enabled")) is not bool):
            raise ValueError("Validator returned unsupported metadata")
        return checked
    except (OSError, ValueError, TypeError, RecursionError, subprocess.TimeoutExpired):
        raise ValueError("The public website connection settings are invalid") from None


def write_bytes(target, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pages-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_site(source=SOURCE, output=OUTPUT, settings=None, node_executable=None):
    source_root, output_root = ordinary(source), ordinary(output)
    if (source_root == output_root or output_root.is_relative_to(source_root)
            or source_root.is_relative_to(output_root)):
        raise ValueError("The output must be separate from the source")
    if not source_root.is_dir():
        raise ValueError("The website source must be an ordinary directory")
    settings = ordinary(settings) if settings is not None else None
    if settings is not None and (settings == output_root or settings.is_relative_to(output_root)):
        raise ValueError("The settings file must be outside the staging directory")
    checked_settings = public_settings(settings, source_root, node_executable)
    files = WEB_FILES + (CONNECTION_FILES if checked_settings["enabled"] else ())

    checked = []
    for name in files:
        path = ordinary(source_root / name)
        if not path.is_file() or not path.resolve().is_relative_to(source_root):
            raise ValueError(f"Missing or invalid static file: {name}")
        checked.append((name, path))

    allowed = set(WEB_FILES) | set(CONNECTION_FILES) | {GENERATED_SETTINGS, ".nojekyll"}
    allowed_directories = {parent.as_posix() for name in allowed for parent in Path(name).parents if parent != Path(".")}
    if output_root.exists():
        if not output_root.is_dir():
            raise ValueError("The staging output must be an ordinary directory")
        pending = [output_root]
        while pending:
            for path in pending.pop().iterdir():
                relative = path.relative_to(output_root).as_posix()
                if linked(path):
                    raise ValueError("The staging directory contains a linked path")
                if path.is_dir():
                    if relative not in allowed_directories:
                        raise ValueError("The staging directory contains unexpected directories")
                    pending.append(path)
                elif not path.is_file() or relative not in allowed:
                    raise ValueError("The staging directory contains unexpected files; use a clean output directory")

    output_root.mkdir(parents=True, exist_ok=True)
    # Remove only previously allowlisted web-connection files when returning to
    # a demo build. Every existing path was checked above; unrelated files are
    # never deleted, copied, or included in the published artifact.
    for name in set(CONNECTION_FILES) - set(files):
        (output_root / name).unlink(missing_ok=True)
    for name, path in checked:
        target = output_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        write_bytes(target, path.read_bytes())
    module = ("// Generated public website metadata. Never put credentials in this file.\n"
              f"export const webClientSettings = Object.freeze({json.dumps(checked_settings, ensure_ascii=True, allow_nan=False)});\n")
    write_bytes(output_root / GENERATED_SETTINGS, module.encode("utf-8"))
    write_bytes(output_root / ".nojekyll", b"")
    print(f"Staged website ({'configured service' if checked_settings['enabled'] else 'demo only'}) in {output_root}")
    return output_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, help="Public website deployment JSON only, never credentials")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="Empty directory or an existing allowlisted staging directory")
    args = parser.parse_args(argv)
    try:
        build_site(output=args.output, settings=args.settings)
    except (ValueError, OSError) as error:
        print(f"Website was not staged: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
