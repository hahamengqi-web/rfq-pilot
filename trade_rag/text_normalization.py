"""Shared Unicode normalization for ingestion and retrieval."""

from __future__ import annotations

import unicodedata


def normalize_search_text(value: object) -> str:
    """Fold compatibility glyphs without changing natural-language content."""
    return unicodedata.normalize("NFKC", str(value or ""))
