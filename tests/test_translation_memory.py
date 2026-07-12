import threading

import pytest

from src.core.translation_memory import CorrectionEntry, TranslationMemory


def test_translation_memory_is_exact_and_language_scoped(tmp_path):
    memory = TranslationMemory(tmp_path / "memory.sqlite3")
    memory.remember("  Ne sautez pas!  ", "Do not jump!", "fr", "en")

    assert memory.lookup("Ne sautez pas!", "fr", "en") == "Do not jump!"
    assert memory.lookup("ne sautez pas!", "fr", "en") is None
    assert memory.lookup("Ne sautez pas!", "fr", "de") is None
    assert memory.count() == 1


def test_translation_memory_doctrine_scope_and_forget(tmp_path):
    memory = TranslationMemory(tmp_path / "memory.sqlite3")
    memory.remember("broadcast armor", "Send armor", "en", "de", "armor-pack")

    assert memory.lookup("broadcast armor", "en", "de", "") is None
    assert memory.lookup("broadcast armor", "en", "de", "armor-pack") == "Send armor"
    assert memory.forget("broadcast armor", "en", "de", "armor-pack") is True
    assert memory.lookup("broadcast armor", "en", "de", "armor-pack") is None


def test_translation_memory_updates_existing_exact_key(tmp_path):
    memory = TranslationMemory(tmp_path / "memory.sqlite3")
    memory.remember("primary Raven", "Old", "en", "fr")
    memory.remember("primary Raven", "New", "en", "fr")

    assert memory.lookup("primary Raven", "en", "fr") == "New"
    assert memory.count() == 1


def test_scoped_lookup_prefers_scope_then_falls_back_globally_in_one_query(tmp_path):
    memory = TranslationMemory(tmp_path / "memory.sqlite3")
    memory.remember("hold", "Global hold", "fr", "en")
    memory.remember("jump", "Doctrine jump", "fr", "en", "armor@1")
    memory._lookup_cache.clear()
    statements = []
    memory._connection.set_trace_callback(statements.append)

    assert memory.lookup("jump", "fr", "en", "armor@1") == "Doctrine jump"
    assert memory.lookup("hold", "fr", "en", "armor@1") == "Global hold"
    assert memory.lookup("missing", "fr", "en", "armor@1") is None
    assert memory.lookup("missing", "fr", "en", "armor@1") is None

    selects = [item for item in statements if item.lstrip().upper().startswith("SELECT")]
    # The scoped hit and fallback each require one query; the repeated miss is cached.
    assert len(selects) == 3


def test_remember_and_forget_invalidate_positive_and_negative_cache(tmp_path):
    memory = TranslationMemory(tmp_path / "memory.sqlite3")

    assert memory.lookup("hold", "fr", "en", "armor@1") is None
    memory.remember("hold", "Global", "fr", "en")
    assert memory.lookup("hold", "fr", "en", "armor@1") == "Global"
    memory.remember("hold", "Scoped", "fr", "en", "armor@1")
    assert memory.lookup("hold", "fr", "en", "armor@1") == "Scoped"
    assert memory.forget("hold", "fr", "en", "armor@1") is True
    assert memory.lookup("hold", "fr", "en", "armor@1") == "Global"


def test_lookup_cache_is_bounded(tmp_path):
    memory = TranslationMemory(tmp_path / "memory.sqlite3", cache_size=2)

    for index in range(3):
        assert memory.lookup(f"missing {index}", "fr", "en") is None

    assert len(memory._lookup_cache) == 2


def test_memory_connection_is_thread_safe_and_has_explicit_lifecycle(tmp_path):
    path = tmp_path / "memory.sqlite3"
    memory = TranslationMemory(path)
    memory.remember("jump", "Jump", "en", "de")
    result = []
    thread = threading.Thread(
        target=lambda: result.append(memory.lookup("jump", "en", "de")),
    )
    thread.start()
    thread.join()

    assert result == ["Jump"]
    memory.close()
    memory.close()
    with pytest.raises(RuntimeError, match="closed"):
        memory.count()


def test_memory_exports_stable_rows_and_bulk_imports(tmp_path):
    memory = TranslationMemory(tmp_path / "memory.sqlite3")
    entries = (
        CorrectionEntry("jump", "Springen", "en", "de"),
        CorrectionEntry("hold", "Halten", "en", "de", "armor@1"),
    )

    assert memory.remember_many(entries) == 2
    assert memory.corrections("") == (entries[0],)
    assert memory.corrections("armor@1") == (entries[1],)
