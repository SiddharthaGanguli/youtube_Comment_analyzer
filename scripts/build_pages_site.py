"""Stage only the static demo files for GitHub Pages.

Run from any directory: python scripts/build_pages_site.py
The output is _site/ at the repository root. No backend or config is published.
"""

from pathlib import Path
from shutil import copyfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension"
OUTPUT = ROOT / "_site"
WEB_FILES = (
    "index.html", "dashboard.html", "privacy.html", "terms.html",
    "styles.css", "home.css", "privacy.css",
    "dashboard.mjs", "dashboard-app.mjs", "demo-data.mjs", "lib.mjs",
    "assets/developed-with-youtube.png",
)


def linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def build_site(source=SOURCE, output=OUTPUT):
    if linked(source) or linked(output):
        raise ValueError("The source and output must be ordinary directories")
    source_root = source.resolve()
    output_root = output.resolve()
    if source_root == output_root or output_root.is_relative_to(source_root):
        raise ValueError("The output must be separate from the source")

    checked = []
    for name in WEB_FILES:
        path = source
        for part in Path(name).parts:
            path /= part
            if linked(path):
                raise ValueError(f"Linked source paths cannot be published: {name}")
        if not path.is_file() or not path.resolve().is_relative_to(source_root):
            raise ValueError(f"Missing or invalid static file: {name}")
        checked.append((name, path))

    allowed = set(WEB_FILES) | {".nojekyll"}
    if output.exists():
        for path in output.rglob("*"):
            if linked(path):
                raise ValueError("The staging directory contains a linked path")
            if path.is_file() and path.relative_to(output).as_posix() not in allowed:
                raise ValueError("The staging directory contains unexpected files; use a clean output directory")

    output.mkdir(parents=True, exist_ok=True)
    for name, path in checked:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        copyfile(path, target)
    (output / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Staged {len(checked)} static demo files in {output}")


if __name__ == "__main__":
    build_site()
