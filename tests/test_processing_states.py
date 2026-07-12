from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.message import ProcessedMessage, TranslationState
from src.core.tokenizer import EVELinkTokenizer
from src.services.processing import LogProcessingWorker, TranslationJob, TranslationScheduler


def _make_worker(*, should_translate, translation=("translated", True, "Test")):
    parser = MagicMock()
    parser.parse.return_value = SimpleNamespace(
        message="source text",
        sender="Pilot",
        timestamp=datetime(2026, 7, 10, 12, 34, 56),
    )
    tokenizer = MagicMock()
    tokenizer.tokenize.return_value = SimpleNamespace(cleaned="source text", tokens={})
    detector = MagicMock()
    detector.should_translate.return_value = should_translate
    translator = MagicMock()
    translator.translate_message.return_value = translation
    translator.set_config = MagicMock()
    return LogProcessingWorker(parser, tokenizer, detector, translator)


def test_untranslated_line_emits_structured_original_and_uses_target_language():
    worker = _make_worker(should_translate=(False, "en"))
    worker.config = {
        "target_language": "en-US",
        "ignored_languages": ["en"],
    }
    emitted = []
    worker.signals.message_ready.connect(emitted.append)

    worker.process_lines("fleet", "source-1", ["line"])

    assert len(emitted) == 1
    assert isinstance(emitted[0], ProcessedMessage)
    assert emitted[0].state == TranslationState.ORIGINAL
    assert emitted[0].text == "source text"
    worker.detector.should_translate.assert_called_once_with(
        "source text",
        target_lang="en-US",
        ignored_langs={"en"},
    )


def test_worker_normalizes_hand_edited_language_settings():
    worker = _make_worker(should_translate=(False, "en"))

    worker.update_config({
        "target_language": 123,
        "ignored_languages": "EN_us",
    })

    assert worker.config["target_language"] == "en"
    assert worker.config["ignored_languages"] == ["en-us"]
    worker.translator_service.set_config.assert_called_once_with(worker.config)


def test_pending_provider_change_defers_remaining_batch_until_applied():
    worker = _make_worker(should_translate=(False, "en"))
    processed = []

    def process_one(session_id, source_id, line):
        processed.append(line)
        if line == "first":
            worker.request_config_update()

    worker._process_single_line = MagicMock(side_effect=process_one)

    worker.process_lines("fleet", "source-1", ["first", "second", "third"])

    assert processed == ["first"]
    assert list(worker._deferred_batches) == [
        ("fleet", "source-1", ["second", "third"])
    ]

    worker.apply_queued_config(1, {"translation_provider": "disabled"})

    assert processed == ["first", "second", "third"]
    assert list(worker._deferred_batches) == []


def test_newer_provider_change_keeps_batch_deferred_until_latest_config():
    worker = _make_worker(should_translate=(False, "en"))
    worker._process_single_line = MagicMock()
    first_generation = worker.request_config_update()
    second_generation = worker.request_config_update()

    worker.process_lines("fleet", "source-1", ["line"])
    worker.apply_queued_config(first_generation, {"translation_provider": "google"})

    worker._process_single_line.assert_not_called()
    assert len(worker._deferred_batches) == 1

    worker.apply_queued_config(second_generation, {"translation_provider": "disabled"})

    worker._process_single_line.assert_called_once_with("fleet", "source-1", "line")
    assert list(worker._deferred_batches) == []


def test_switching_to_disabled_prevents_later_lines_using_old_provider():
    worker = _make_worker(
        should_translate=(True, "fr"),
        translation=("translated", True, "Google"),
    )

    def first_provider_call(*args, **kwargs):
        worker.request_config_update()
        return "translated", True, "Google"

    def apply_provider_config(config):
        if config.get("translation_provider") == "disabled":
            worker.translator_service.get_provider_status.return_value = {
                "mode": "disabled",
                "name": "Disabled",
                "available": False,
                "reason": "Translation provider is disabled.",
            }

    worker.translator_service.translate_message.side_effect = first_provider_call
    worker.translator_service.set_config.side_effect = apply_provider_config

    worker.process_lines("fleet", "source-1", ["first", "second"])
    assert worker.translator_service.translate_message.call_count == 1

    worker.apply_queued_config(1, {"translation_provider": "disabled"})

    assert worker.translator_service.translate_message.call_count == 1


def test_translated_line_emits_pending_then_success_for_same_message():
    worker = _make_worker(
        should_translate=(True, "fr"),
        translation=("translated text", True, "Test Provider"),
    )
    emitted = []
    worker.signals.message_ready.connect(emitted.append)

    worker.process_lines("fleet", "source-1", ["line"])

    assert [message.state for message in emitted] == [
        TranslationState.PENDING,
        TranslationState.TRANSLATED,
    ]
    assert emitted[0].message_id == emitted[1].message_id
    assert emitted[0].text == "source text"
    assert emitted[1].text == "translated text"
    assert emitted[1].original_text == "source text"
    assert emitted[1].provider == "Test Provider"


def test_failed_translation_replaces_pending_with_truthful_original():
    worker = _make_worker(
        should_translate=(True, "fr"),
        translation=("source text", False, "DeepL"),
    )
    emitted = []
    worker.signals.message_ready.connect(emitted.append)

    worker.process_lines("fleet", "source-1", ["line"])

    assert [message.state for message in emitted] == [
        TranslationState.PENDING,
        TranslationState.FAILED,
    ]
    assert emitted[-1].text == "source text"
    assert emitted[-1].original_text == ""
    assert emitted[-1].error == "DeepL translation unavailable"


def test_disabled_provider_is_reported_explicitly():
    worker = _make_worker(
        should_translate=(True, "fr"),
        translation=("source text", False, "Disabled"),
    )
    worker.translator_service.get_provider_status.return_value = {
        "mode": "disabled",
        "name": "Disabled",
        "available": False,
        "reason": "Translation provider is disabled.",
    }
    emitted = []
    worker.signals.message_ready.connect(emitted.append)

    worker.process_lines("fleet", "source-1", ["line"])

    assert [message.state for message in emitted] == [TranslationState.ORIGINAL]
    assert emitted[-1].error == ""
    worker.translator_service.translate_message.assert_not_called()


def test_unexpected_provider_exception_resolves_pending_as_failed():
    worker = _make_worker(should_translate=(True, "fr"))
    worker.translator_service.provider_name = "Test Provider"
    worker.translator_service.translate_message.side_effect = RuntimeError("offline")
    emitted = []
    worker.signals.message_ready.connect(emitted.append)

    worker.process_lines("fleet", "source-1", ["line"])

    assert [message.state for message in emitted] == [
        TranslationState.PENDING,
        TranslationState.FAILED,
    ]
    assert emitted[-1].text == "source text"
    assert emitted[-1].error == "Test Provider translation unavailable"


def test_deferred_provider_pipeline_emits_every_original_before_any_provider_call():
    parser = MagicMock()
    parser.parse.side_effect = [
        SimpleNamespace(
            message=text,
            sender="Fleet Boss",
            timestamp=datetime(2026, 7, 10, 12, 34, index),
        )
        for index, text in enumerate(("bonjour", "salut"), start=1)
    ]
    tokenizer = MagicMock()
    tokenizer.tokenize.side_effect = [
        SimpleNamespace(cleaned="bonjour", tokens={}),
        SimpleNamespace(cleaned="salut", tokens={}),
    ]
    detector = MagicMock()
    detector.should_translate.return_value = (True, "fr")
    translator = MagicMock()
    worker = LogProcessingWorker(
        parser,
        tokenizer,
        detector,
        translator,
        defer_translation=True,
    )
    worker.update_config({
        "translation_provider": "google",
        "target_language": "en",
        "ignored_languages": ["en"],
        "speaker_roles": {"fleet boss": "fc"},
    })
    messages = []
    jobs = []
    worker.signals.message_ready.connect(messages.append)
    worker.signals.translation_requested.connect(jobs.append)

    worker.process_lines("fleet", "source", ["one", "two"])

    assert [item.state for item in messages] == [
        TranslationState.PENDING,
        TranslationState.PENDING,
    ]
    assert [job.source_text for job in jobs] == ["bonjour", "salut"]
    assert all(item.speaker_role == "fc" for item in messages)
    translator.translate_message.assert_not_called()
    translator.set_config.assert_not_called()


def test_scheduler_preserves_batch_order_and_context_scope():
    service = MagicMock()
    service.entity_index = None
    service.translate_batch_messages.return_value = [
        ("one translated", True, "DeepL"),
        ("two translated", True, "DeepL"),
    ]
    scheduler = TranslationScheduler(service)
    scheduler.config.update({"context_assist": True, "context_messages": 3})
    emitted = []
    scheduler.message_ready.connect(emitted.append)
    jobs = [
        TranslationJob(
            message_id=f"m{index}", session_id="fleet", source_id="source-a",
            source_text=text, final_original=text, sender="Pilot",
            timestamp="12:00:00", detected_language="fr", target_language="en",
            tokens={},
        )
        for index, text in enumerate(("one", "two"), start=1)
    ]

    scheduler._translate_jobs(jobs)

    assert [item.message_id for item in emitted] == ["m1", "m2"]
    assert [item.text for item in emitted] == ["one translated", "two translated"]
    assert scheduler._context_for(jobs[0]) == "one\ntwo"
    other_source = replace_job(jobs[0], source_id="source-b")
    assert scheduler._context_for(other_source) is None


def test_command_classifier_uses_visible_label_from_eve_link_controls():
    parser = MagicMock()
    parser.parse.return_value = SimpleNamespace(
        message="align \x01Jita\x02",
        sender="Fleet Boss",
        timestamp=datetime(2026, 7, 10, 12, 34, 56),
    )
    detector = MagicMock()
    detector.should_translate.return_value = (False, "en")
    translator = MagicMock()
    worker = LogProcessingWorker(
        parser,
        EVELinkTokenizer(),
        detector,
        translator,
    )
    emitted = []
    worker.signals.message_ready.connect(emitted.append)

    worker.process_lines("fleet", "source", ["line"])

    assert [(item.code, item.argument) for item in emitted[0].commands] == [
        ("ALIGN", "Jita"),
    ]
    assert "Jita" in emitted[0].text


def test_scheduler_retargets_jobs_already_waiting_when_config_applies():
    service = MagicMock()
    service.entity_index = None
    scheduler = TranslationScheduler(service)
    scheduler.config['burst_optimization'] = True
    job = TranslationJob(
        message_id="m1", session_id="fleet", source_id="source",
        source_text="bonjour", final_original="bonjour", sender="Pilot",
        timestamp="12:00:00", detected_language="fr", target_language="en",
        tokens={}, config_generation=0,
    )
    scheduler.queue_job(job)
    assert len(scheduler._jobs) == 1

    scheduler.request_config_update(1)
    scheduler.apply_queued_config(1, {
        'target_language': 'de',
        'translation_provider': 'disabled',
        'burst_optimization': True,
    })

    retargeted = scheduler._jobs[0]
    assert retargeted.target_language == 'de'
    assert retargeted.config_generation == 1
    scheduler.stop()


def test_scheduler_immediately_retargets_late_stale_job_after_config_applied():
    service = MagicMock()
    service.entity_index = None
    scheduler = TranslationScheduler(service)
    scheduler.request_config_update(1)
    scheduler.apply_queued_config(1, {
        'target_language': 'de',
        'translation_provider': 'disabled',
        'burst_optimization': True,
    })
    late_job = TranslationJob(
        message_id="late", session_id="fleet", source_id="source",
        source_text="bonjour", final_original="bonjour", sender="Pilot",
        timestamp="12:00:00", detected_language="fr", target_language="en",
        tokens={}, config_generation=0,
    )

    scheduler.queue_job(late_job)

    assert not scheduler._deferred_jobs
    assert scheduler._jobs[0].target_language == 'de'
    assert scheduler._jobs[0].config_generation == 1
    scheduler.stop()


def replace_job(job, **changes):
    values = dict(job.__dict__)
    values.update(changes)
    return TranslationJob(**values)
