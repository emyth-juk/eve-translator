"""Work the pipeline no longer repeats: hidden lines, cache wipes, context resets."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.combat import classify_commands
from src.core.message import TranslationState
from src.services.processing import (
    LogProcessingWorker,
    TranslationJob,
    TranslationScheduler,
)
from src.services.translator import TranslationService


def _worker(message_text):
    parser = MagicMock()
    parser.parse.return_value = SimpleNamespace(
        message=message_text,
        sender="Pilot",
        timestamp=SimpleNamespace(strftime=lambda _fmt: "12:00:00"),
    )
    tokenizer = MagicMock()
    tokenizer.tokenize.return_value = SimpleNamespace(
        cleaned=message_text,
        tokens={},
    )
    detector = MagicMock()
    detector.should_translate.return_value = (True, "fr")
    worker = LogProcessingWorker(
        parser,
        tokenizer,
        detector,
        MagicMock(),
        defer_translation=True,
    )
    worker.update_config({
        "translation_provider": "google",
        "provider_choice_confirmed": True,
        "target_language": "en",
    })
    return worker


def test_combat_mode_does_not_request_translation_for_hidden_lines():
    worker = _worker("bonjour tout le monde")
    worker.set_session_display_modes({"fleet": "combat"})
    messages, jobs = [], []
    worker.signals.message_ready.connect(messages.append)
    worker.signals.translation_requested.connect(jobs.append)

    worker.process_lines("fleet", "source", ["line"])

    assert jobs == []
    assert [message.state for message in messages] == [TranslationState.ORIGINAL]


def test_combat_mode_still_translates_command_lines():
    worker = _worker("align to gate")
    worker.set_session_display_modes({"fleet": "combat"})
    jobs = []
    worker.signals.translation_requested.connect(jobs.append)

    worker.process_lines("fleet", "source", ["line"])

    assert len(jobs) == 1
    assert jobs[0].commands == tuple(classify_commands("align to gate"))


def test_mirror_mode_translates_every_relevant_line():
    worker = _worker("bonjour tout le monde")
    worker.set_session_display_modes({"fleet": "mirror"})
    jobs = []
    worker.signals.translation_requested.connect(jobs.append)

    worker.process_lines("fleet", "source", ["line"])

    assert len(jobs) == 1


def test_reapplying_the_same_entity_index_keeps_translation_caches():
    index = MagicMock()
    service = TranslationService(entity_index=index)
    service._translation_cache["key"] = "cached"
    service._lexicon_matchers["key"] = "matcher"

    service.set_entity_index(index)

    assert service._translation_cache == {"key": "cached"}
    assert service._lexicon_matchers == {"key": "matcher"}

    service.set_entity_index(MagicMock())
    assert service._translation_cache == {}


def test_reloading_the_same_doctrine_revision_keeps_caches():
    service = TranslationService()
    service.set_doctrine_pack(SimpleNamespace(revision="fleet@1:abc"))
    service._translation_cache["key"] = "cached"

    service.set_doctrine_pack(SimpleNamespace(revision="fleet@1:abc"))

    assert service._translation_cache == {"key": "cached"}


def test_speaker_role_config_change_keeps_rolling_context():
    service = MagicMock()
    service.entity_index = None
    scheduler = TranslationScheduler(service)
    scheduler.update_config({"context_assist": True, "target_language": "en"})
    job = TranslationJob(
        message_id="m", session_id="fleet", source_id="s", source_text="bonjour",
        final_original="bonjour", sender="Pilot", timestamp="12:00:00",
        detected_language="fr", target_language="en", tokens={},
    )
    scheduler._remember_context(job)

    scheduler.update_config({
        "context_assist": True,
        "target_language": "en",
        "speaker_roles": {"boss": "fc"},
    })
    assert scheduler._context_for(job) == "bonjour"

    scheduler.update_config({"context_assist": True, "target_language": "de"})
    assert scheduler._context_for(job) is None
