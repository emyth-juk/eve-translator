"""Exact, local-only translation corrections.

Corrections deliberately use exact normalized source text. Fuzzy matching is
unsafe for short combat calls where punctuation and negation can change intent.
"""

from __future__ import annotations

import sqlite3
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.core.languages import normalize_locale


SCHEMA_VERSION = 1
DEFAULT_CACHE_SIZE = 1_024
_NOT_CACHED = object()


class TranslationMemorySchemaError(RuntimeError):
    """Raised before writes when a correction DB comes from a newer app."""


def normalize_correction_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    return " ".join(normalized.split())


def normalize_language(language: str | None, default: str = "unknown") -> str:
    """Compatibility wrapper for the app's shared locale normalizer."""

    return normalize_locale(language, default)


@dataclass(frozen=True)
class CorrectionEntry:
    """One normalized, exact correction that can be exported safely."""

    source_text: str
    translated_text: str
    source_language: str
    target_language: str
    doctrine_scope: str = ""


class TranslationMemory:
    """Thread-safe SQLite store with bounded positive and negative caching.

    A single guarded connection avoids opening SQLite for every fleet-chat
    line. ``check_same_thread=False`` is safe here because every database and
    cache operation is serialized by ``_lock``. Call :meth:`close` when an
    owning application has an explicit shutdown lifecycle; the context-manager
    protocol provides the same guarantee for scripts and tests.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ):
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")
        self.path = Path(path) if path else (
            Path.home() / ".eve_translator" / "translation_memory.sqlite3"
        )
        self._lock = threading.RLock()
        self._initialized = False
        self._closed = False
        self._connection: sqlite3.Connection | None = None
        self._cache_size = cache_size
        self._lookup_cache: OrderedDict[
            tuple[str, str, str, str], str | object
        ] = OrderedDict()

    def __enter__(self) -> TranslationMemory:
        with self._lock:
            self._assert_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("TranslationMemory is closed")

    def _connection_locked(self) -> sqlite3.Connection:
        self._assert_open()
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self.path,
                timeout=3.0,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA busy_timeout = 3000")
        return self._connection

    def close(self) -> None:
        """Close the persistent SQLite connection; safe to call repeatedly."""

        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._lookup_cache.clear()
            self._closed = True

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            connection = self._connection_locked()
            try:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is not None:
                try:
                    existing_version = int(row[0])
                except (TypeError, ValueError) as exc:
                    raise TranslationMemorySchemaError(
                        "Correction memory has an invalid schema version"
                    ) from exc
                if existing_version > SCHEMA_VERSION:
                    raise TranslationMemorySchemaError(
                        "Correction memory was created by a newer EVE Translator "
                        f"(schema {existing_version}); refusing to downgrade it"
                    )
            with connection:
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
                    "INSERT OR REPLACE INTO metadata(key, value) "
                    "VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            self._initialized = True

    @staticmethod
    def _key(
        source_text: str,
        source_language: str,
        target_language: str,
        doctrine_scope: str,
    ) -> tuple[str, str, str, str]:
        return (
            normalize_language(source_language),
            normalize_language(target_language, "en"),
            normalize_correction_text(doctrine_scope),
            normalize_correction_text(source_text),
        )

    def _cache_get_locked(self, key: tuple[str, str, str, str]) -> str | None | object:
        if not self._cache_size or key not in self._lookup_cache:
            return _NOT_CACHED
        value = self._lookup_cache.pop(key)
        self._lookup_cache[key] = value
        return value

    def _cache_put_locked(
        self,
        key: tuple[str, str, str, str],
        value: str | None,
    ) -> None:
        if not self._cache_size:
            return
        self._lookup_cache.pop(key, None)
        self._lookup_cache[key] = value
        while len(self._lookup_cache) > self._cache_size:
            self._lookup_cache.popitem(last=False)

    def _invalidate_locked(self, key: tuple[str, str, str, str]) -> None:
        source_language, target_language, doctrine_scope, source_text = key
        if doctrine_scope:
            self._lookup_cache.pop(key, None)
            return
        # A global correction is the fallback for every doctrine scope.
        for cached_key in tuple(self._lookup_cache):
            if (
                cached_key[0] == source_language
                and cached_key[1] == target_language
                and cached_key[3] == source_text
            ):
                self._lookup_cache.pop(cached_key, None)

    def lookup(
        self,
        source_text: str,
        source_language: str,
        target_language: str,
        doctrine_scope: str = "",
    ) -> str | None:
        """Return a scoped correction, falling back globally in one SQL query.

        An empty scope is an exact global lookup. A non-empty scope selects its
        own correction first and the global fallback second.
        """

        key = self._key(
            source_text,
            source_language,
            target_language,
            doctrine_scope,
        )
        source_language, target_language, scope, source = key
        if not source:
            return None
        self._ensure_schema()
        with self._lock:
            cached = self._cache_get_locked(key)
            if cached is not _NOT_CACHED:
                return str(cached) if isinstance(cached, str) else None
            connection = self._connection_locked()
            if scope:
                row = connection.execute(
                    """
                    SELECT translated_text
                    FROM corrections
                    WHERE source_language = ? AND target_language = ?
                      AND source_text = ? AND doctrine_scope IN (?, '')
                    ORDER BY CASE WHEN doctrine_scope = ? THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (source_language, target_language, source, scope, scope),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT translated_text
                    FROM corrections
                    WHERE source_language = ? AND target_language = ?
                      AND doctrine_scope = ? AND source_text = ?
                    LIMIT 1
                    """,
                    (source_language, target_language, scope, source),
                ).fetchone()
            result = str(row[0]) if row else None
            self._cache_put_locked(key, result)
            return result

    @staticmethod
    def _normalize_entry(entry: CorrectionEntry) -> CorrectionEntry:
        if not isinstance(entry, CorrectionEntry):
            raise TypeError("entries must contain CorrectionEntry values")
        source = normalize_correction_text(entry.source_text)
        translated = normalize_correction_text(entry.translated_text)
        if not source or not translated:
            raise ValueError("Source text and corrected translation are required")
        return CorrectionEntry(
            source_text=source,
            translated_text=translated,
            source_language=normalize_language(entry.source_language),
            target_language=normalize_language(entry.target_language, "en"),
            doctrine_scope=normalize_correction_text(entry.doctrine_scope),
        )

    def remember_many(self, entries: Iterable[CorrectionEntry]) -> int:
        """Store normalized entries in one transaction and return their count."""

        normalized = tuple(self._normalize_entry(entry) for entry in entries)
        if not normalized:
            return 0
        self._ensure_schema()
        with self._lock:
            connection = self._connection_locked()
            with connection:
                connection.executemany(
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
                        (
                            entry.source_language,
                            entry.target_language,
                            entry.doctrine_scope,
                            entry.source_text,
                            entry.translated_text,
                        )
                        for entry in normalized
                    ),
                )
            for entry in normalized:
                key = (
                    entry.source_language,
                    entry.target_language,
                    entry.doctrine_scope,
                    entry.source_text,
                )
                self._invalidate_locked(key)
                self._cache_put_locked(key, entry.translated_text)
        return len(normalized)

    def remember(
        self,
        source_text: str,
        translated_text: str,
        source_language: str,
        target_language: str,
        doctrine_scope: str = "",
    ) -> None:
        self.remember_many((CorrectionEntry(
            source_text=source_text,
            translated_text=translated_text,
            source_language=source_language,
            target_language=target_language,
            doctrine_scope=doctrine_scope,
        ),))

    def forget(
        self,
        source_text: str,
        source_language: str,
        target_language: str,
        doctrine_scope: str = "",
    ) -> bool:
        key = self._key(
            source_text,
            source_language,
            target_language,
            doctrine_scope,
        )
        self._ensure_schema()
        with self._lock:
            connection = self._connection_locked()
            with connection:
                cursor = connection.execute(
                    """
                    DELETE FROM corrections
                    WHERE source_language = ? AND target_language = ?
                      AND doctrine_scope = ? AND source_text = ?
                    """,
                    key,
                )
            self._invalidate_locked(key)
            return cursor.rowcount > 0

    def corrections(self, doctrine_scope: str | None = None) -> tuple[CorrectionEntry, ...]:
        """Return stable export rows, optionally restricted to one exact scope."""

        self._ensure_schema()
        with self._lock:
            connection = self._connection_locked()
            if doctrine_scope is None:
                rows = connection.execute(
                    """
                    SELECT source_text, translated_text, source_language,
                           target_language, doctrine_scope
                    FROM corrections
                    ORDER BY doctrine_scope, source_language, target_language, source_text
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT source_text, translated_text, source_language,
                           target_language, doctrine_scope
                    FROM corrections
                    WHERE doctrine_scope = ?
                    ORDER BY source_language, target_language, source_text
                    """,
                    (normalize_correction_text(doctrine_scope),),
                ).fetchall()
        return tuple(CorrectionEntry(*map(str, row)) for row in rows)

    def count(self) -> int:
        self._ensure_schema()
        with self._lock:
            row = self._connection_locked().execute(
                "SELECT COUNT(*) FROM corrections"
            ).fetchone()
        return int(row[0]) if row else 0
