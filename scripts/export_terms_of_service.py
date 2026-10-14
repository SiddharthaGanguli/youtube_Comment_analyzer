# /// script
# requires-python = ">=3.12"
# dependencies = ["reportlab==5.0.1"]
# ///
"""Export the actual terms page as an audit upload PDF.

Run from the repository root: uv run scripts/export_terms_of_service.py
"""

from export_privacy_policy import ROOT, export_document


if __name__ == "__main__":
    export_document(
        ROOT / "extension" / "terms.html",
        ROOT / "output" / "pdf" / "comment-lens-terms-of-service.pdf",
        title="Comment Lens Terms of Service",
        subject="Terms for the current preview and planned live comment analysis",
        footer="Terms of Service | Development preview | 14 October 2026",
    )
