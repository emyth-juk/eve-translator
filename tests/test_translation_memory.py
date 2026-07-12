from src.core.translation_memory import TranslationMemory


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
