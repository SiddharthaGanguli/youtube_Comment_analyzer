"""Build a local-only preview of the real dashboard using synthetic job states.

This does not change the public site, extension settings, or any AWS resources.
The generated directory can be served with Python's local HTTP server.
"""

import argparse
import json
from pathlib import Path
import sys
import tempfile

if __package__:
    from .build_extension_bundle import (BUNDLE_FILES, ROOT, SOURCE, _linked,
                                         _ordinary_path, _write_bytes, build_extension_bundle)
else:
    from build_extension_bundle import (BUNDLE_FILES, ROOT, SOURCE, _linked,
                                        _ordinary_path, _write_bytes, build_extension_bundle)


OUTPUT = ROOT / "artifacts" / "extension-fixture"
FIXTURE_MODULES = ("fixture-data.mjs", "fixture-preview.mjs")
MARKER_FILE = ".comment-lens-fixture.json"
MARKER = {"builder": "comment-lens-extension-fixture", "schema_version": 1}
FILES = BUNDLE_FILES | set(FIXTURE_MODULES) | {MARKER_FILE}
WARNING = """<aside id="fixture-warning" role="note" style="position:sticky;top:0;z-index:100;background:#fff3cd;color:#503600;border-bottom:2px solid #c58b00;padding:12px 20px;display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px">
<strong>DEVELOPER FIXTURE TEST — no AWS or YouTube calls</strong>
<span id="fixture-description">All accounts and analysis data are synthetic.</span>
<label style="margin-left:auto">Scenario <select id="fixture-scenario" aria-label="Fixture scenario"></select></label>
</aside>"""


def _check_fixture_output(output):
    if not output.exists():
        return
    if not output.is_dir():
        raise ValueError("The fixture output must be a directory")
    if not any(output.iterdir()):
        return
    allowed_dirs = {parent.as_posix() for name in FILES for parent in Path(name).parents if parent != Path(".")}
    pending = [output]
    while pending:
        for path in pending.pop().iterdir():
            if _linked(path):
                raise ValueError("The fixture output must not contain symlinks or junctions")
            relative = path.relative_to(output).as_posix()
            if path.is_dir():
                if relative not in allowed_dirs:
                    raise ValueError("The fixture output contains unexpected directories")
                pending.append(path)
            elif not path.is_file() or relative not in FILES:
                raise ValueError("The fixture output contains unexpected files")
    try:
        marker = output / MARKER_FILE
        if marker.stat().st_size > 1024 or json.loads(marker.read_text(encoding="utf-8")) != MARKER:
            raise ValueError
    except (OSError, ValueError):
        raise ValueError("Use an empty directory or an existing owned fixture preview") from None


def build_fixture_preview(source=SOURCE, output=OUTPUT):
    source, output = _ordinary_path(source), _ordinary_path(output)
    if source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("The fixture source and output must not overlap")
    _check_fixture_output(output)
    modules = {}
    for name in FIXTURE_MODULES:
        path = _ordinary_path(source / "tests" / name)
        if not path.is_file() or not path.is_relative_to(source):
            raise ValueError("A fixture preview module is missing or invalid")
        modules[name] = path.read_bytes()

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".extension-fixture-", dir=output.parent) as directory:
        staged = build_extension_bundle(source=source, output=Path(directory) / "bundle")
        html = (staged / "dashboard.html").read_text(encoding="utf-8")
        original = '<script type="module" src="dashboard.mjs"></script>'
        if html.count(original) != 1 or html.count("<body>") != 1 or html.count("<head>") != 1:
            raise ValueError("The dashboard entry point could not be replaced safely")
        html = html.replace(original, '<script type="module" src="fixture-preview.mjs"></script>')
        html = html.replace("<head>", '<head>\n  <link rel="icon" href="data:,">', 1)
        html = html.replace("<body>", "<body>\n" + WARNING, 1)

        output.mkdir(parents=True, exist_ok=True)
        _write_bytes(output / MARKER_FILE, (json.dumps(MARKER, indent=2) + "\n").encode("utf-8"))
        for name in BUNDLE_FILES:
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes(target, (staged / name).read_bytes())
        for name, contents in modules.items():
            _write_bytes(output / name, contents)
        _write_bytes(output / "dashboard.html", html.encode("utf-8"))
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT, help="Empty directory or an owned fixture preview")
    args = parser.parse_args(argv)
    try:
        output = build_fixture_preview(output=args.output)
    except (ValueError, OSError) as error:
        print(f"Fixture preview was not built: {error}", file=sys.stderr)
        return 1
    print(f"Built developer-only fixture preview in {output}")
    print(f'python -m http.server 8766 --bind 127.0.0.1 --directory "{output}"')
    print("Open http://127.0.0.1:8766/dashboard.html?scenario=success")
    print("Accept the test policy checkbox, choose Use fixture account, then Simulate test run.")
    print("All states and data are synthetic. No AWS service or YouTube API is called.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
