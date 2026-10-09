"""The text preparation used by both dataset preprocessing and prediction."""

import html
import re
import unicodedata


WHITESPACE = re.compile(r"\s+")


def text_preprocessing_settings(parameters):
    """Save the recipe alongside the trained model, excluding split settings."""
    settings = {
        "version": 1,
        "unicode_normalization": parameters["unicode_normalization"],
        "html_unescape": True,
        "collapse_whitespace": True,
        "language_policy": parameters["language_policy"],
    }
    validate_text_preprocessing(settings)
    return settings


def validate_text_preprocessing(settings):
    expected = {"version", "unicode_normalization", "html_unescape", "collapse_whitespace", "language_policy"}
    if not isinstance(settings, dict) or set(settings) != expected:
        raise ValueError("Model text preprocessing metadata is missing or unsupported; retrain the model")
    if (settings["version"] != 1 or settings["unicode_normalization"] not in {"NFC", "NFKC"}
            or settings["html_unescape"] is not True or settings["collapse_whitespace"] is not True
            or settings["language_policy"] != "keep_all"):
        raise ValueError("Unsupported model text preprocessing recipe")


def normalize_text(text, form="NFKC"):
    """Decode HTML once and keep negation, punctuation, emoji, and Unicode words."""
    if not isinstance(text, str):
        raise TypeError("Comment text must be a string")
    if form not in {"NFC", "NFKC"}:
        raise ValueError("Use NFC or NFKC for Unicode normalization")
    return WHITESPACE.sub(" ", unicodedata.normalize(form, html.unescape(text))).strip()
