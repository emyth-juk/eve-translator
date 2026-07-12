"""Shared language and locale normalization helpers.

The app needs two related, but deliberately distinct, representations:

* ``normalize_locale`` preserves regional subtags for glossary/provider lookup.
* ``normalize_language_code`` collapses a locale to its comparable base
  language for detection and ignore decisions.

Keeping both operations here prevents config, detection, glossary, correction,
and entity-protection paths from slowly developing incompatible rules.
"""

from __future__ import annotations

import unicodedata


LANGUAGE_ALIASES = {
    # Legacy ISO codes occasionally returned by libraries or stored in config.
    "iw": "he",
    "in": "id",
    "ji": "yi",
    # langdetect reports Norwegian as "no", while providers/config may use NB/NN.
    "nb": "no",
    "nn": "no",
}


def normalize_locale(value: object, default: str = "") -> str:
    """Return a lowercase, hyphenated locale while preserving its subtags."""

    if not isinstance(value, str):
        return default
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = normalized.replace("_", "-")
    return normalized or default


def normalize_language_code(value: object, default: str = "unknown") -> str:
    """Return a comparable base language, including known legacy aliases."""

    normalized = normalize_locale(value, default)
    if normalized in {"auto", "unknown"}:
        return normalized
    base_language = normalized.split("-", 1)[0]
    return LANGUAGE_ALIASES.get(base_language, base_language)
