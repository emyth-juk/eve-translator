"""Exact, local-only translation corrections.

Corrections deliberately use exact normalized source text.  Fuzzy matching is
unsafe for short combat calls where punctuation and negation can change intent.
"""

from __future__ import annotations

import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 1


def normalize_correction_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    return " ".join(normalized.split())


def normalize_language(language: str | None, default: str = "unknown") -> str:
    if not isinstance(language, str):
        return default
    normalized = language.strip().lower().replace("_", "-")
    return normalized or default


class TranslationMemory:
    """Small SQLite store safe to use from the processing worker thread."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else (
            Path.home() / ".eve_translator" / "translation_memory.sqlite3"
        )
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3.0)
        connection.execute("PRAGMA busy_timeout = 3000")
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self._session() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS corrections (
                        source_language TEXT NOT NULL,
                        target_language TEXT NOT NULL,
                        doctrine_scope TEXT NOT NULL DEFAULT '',
                        source_text TEXT NOT NULL,
                        translated_text TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (
                            source_language,
                            target_language,
                            doctrine_scope,
                            source_text
                        )
                    );
                    """
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            self._initialized = True

    def lookup(
        self,
        source_text: str,
        source_language: str,
        target_language: str,
        doctrine_scope: str = "",
    ) -> str | None:
        source = normalize_correction_text(source_text)
        if not source:
            return None
        self._ensure_schema()
        with self._lock, self._session() as connection:
            row = connection.execute(
                """
                SELECT translated_text
                FROM corrections
                WHERE source_language = ? AND target_language = ?
                  AND doctrine_scope = ? AND source_text = ?
                """,
                (
                    normalize_language(source_language),
                    normalize_language(target_language, "en"),
                    normalize_correction_text(doctrine_scope),
                    source,
                ),
            ).fetchone()
        return str(row[0]) if row else None

    def remember(
        self,
        source_text: str,
        translated_text: str,
        source_language: str,
        target_language: str,
        doctrine_scope: str = "",
    ) -> None:
        source = normalize_correction_text(source_text)
        translated = normalize_correction_text(translated_text)
        if not source or not translated:
            raise ValueError("Source text and corrected translation are required")
        self._ensure_schema()
        with self._lock, self._session() as connection:
            connection.execute(
                """
                INSERT INTO corrections(
                    source_language, target_language, doctrine_scope,
                    source_text, translated_text
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(
                    source_language, target_language, doctrine_scope, source_text
                ) DO UPDATE SET
                    translated_text = excluded.translated_text,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    normalize_language(source_language),
                    normalize_language(target_language, "en"),
                    normalize_correction_text(doctrine_scope),
                    source,
                    translated,
                ),
            )

    def forget(
        self,
        source_text: str,
        source_language: str,
        target_language: str,
        doctrine_scope: str = "",
    ) -> bool:
        self._ensure_schema()
        with self._lock, self._session() as connection:
            cursor = connection.execute(
                """
                DELETE FROM corrections
                WHERE source_language = ? AND target_language = ?
                  AND doctrine_scope = ? AND source_text = ?
                """,
                (
                    normalize_language(source_language),
                    normalize_language(target_language, "en"),
                    normalize_correction_text(doctrine_scope),
                    normalize_correction_text(source_text),
                ),
            )
            return cursor.rowcount > 0

    def count(self) -> int:
        self._ensure_schema()
        with self._lock, self._session() as connection:
            row = connection.execute("SELECT COUNT(*) FROM corrections").fetchone()
        return int(row[0]) if row else 0
