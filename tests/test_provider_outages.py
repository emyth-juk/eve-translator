"""Provider outage behaviour: bounded stalls, visible reasons, no retry storms."""

import time
from unittest.mock import MagicMock, patch

import requests

from src.core.entity_protection import ProtectedEntity, ProtectedEntityIndex
from src.core.message import TranslationState
from src.services import translator as translator_module
from src.services.processing import (
    MAX_TRANSLATION_JOB_AGE_SECONDS,
    STALE_TRANSLATION_ERROR,
    TranslationJob,
    TranslationScheduler,
)
from src.services.translator import (
    DEEPL_MAX_NETWORK_RETRIES,
    FAILURE_AUTH,
    FAILURE_NETWORK,
    FAILURE_PROTECTED_NAMES,
    FAILURE_QUOTA,
    FAILURE_RATE_LIMIT,
    FAILURE_TIMEOUT,
    PROVIDER_FAILURE_THRESHOLD,
    DeepLProvider,
    ProviderCallTimeout,
    TranslationService,
    describe_provider_failure,
)


class QuotaExceededException(Exception):
    """Same class name as the DeepL SDK exception."""


class AuthorizationException(Exception):
    """Same class name as the DeepL SDK exception."""


class FailingProvider:
    name = "DeepL"
    supports_batch = True
    supports_context = False

    def __init__(self, reason=FAILURE_NETWORK):
        self.single_calls = 0
        self.batch_calls = 0
        self.last_failure_reason = ""
        self.reason = reason

    def translate(self, text, target_lang="en", source_lang=None):
        self.single_calls += 1
        self.last_failure_reason = self.reason
        return None

    def translate_batch(self, texts, target_lang="en", source_lang=None):
        self.batch_calls += 1
        self.last_failure_reason = self.reason
        return None


def _job(message_id="m1", **changes):
    values = dict(
        message_id=message_id,
        session_id="fleet",
        source_id="source",
        source_text="bonjour",
        final_original="bonjour",
        sender="Pilot",
        timestamp="12:00:00",
        detected_language="fr",
        target_language="en",
        tokens={},
    )
    values.update(changes)
    return TranslationJob(**values)


def test_provider_failures_are_classified_without_message_content():
    response = requests.Response()
    response.status_code = 429

    assert describe_provider_failure(QuotaExceededException("x")) == FAILURE_QUOTA
    assert describe_provider_failure(AuthorizationException("x")) == FAILURE_AUTH
    assert describe_provider_failure(
        requests.exceptions.HTTPError(response=response)
    ) == FAILURE_RATE_LIMIT
    assert describe_provider_failure(requests.exceptions.ConnectTimeout()) == FAILURE_TIMEOUT
    assert describe_provider_failure(requests.exceptions.ConnectionError()) == FAILURE_NETWORK
    assert describe_provider_failure(ProviderCallTimeout()) == FAILURE_TIMEOUT
    assert describe_provider_failure(RuntimeError("secret text")) == "request failed"


def test_repeated_failures_pause_the_provider_and_report_why():
    provider = FailingProvider()
    service = TranslationService(provider=provider)

    for index in range(PROVIDER_FAILURE_THRESHOLD):
        assert service.translate_message(f"bonjour {index}", "en", "fr")[1] is False
    assert provider.single_calls == PROVIDER_FAILURE_THRESHOLD

    result = service.translate_message("encore", "en", "fr")

    assert result == ("encore", False, "DeepL")
    assert provider.single_calls == PROVIDER_FAILURE_THRESHOLD  # paused, not called
    assert service.last_failure_reason.startswith(FAILURE_NETWORK)
    status = service.get_provider_status()
    assert status["available"] is False
    assert "DeepL paused: network error" in status["reason"]


def test_paused_provider_probes_again_after_cooldown():
    provider = FailingProvider()
    service = TranslationService(provider=provider)
    for index in range(PROVIDER_FAILURE_THRESHOLD):
        service.translate_message(f"bonjour {index}", "en", "fr")

    later = (
        time.monotonic()
        + translator_module.PROVIDER_FAILURE_COOLDOWN_SECONDS
        + 1
    )
    with patch.object(translator_module.time, "monotonic", return_value=later):
        service.translate_message("probe", "en", "fr")

    assert provider.single_calls == PROVIDER_FAILURE_THRESHOLD + 1


def test_quota_failure_pauses_immediately():
    provider = FailingProvider(reason=FAILURE_QUOTA)
    service = TranslationService(provider=provider)

    service.translate_message("bonjour", "en", "fr")
    service.translate_message("encore", "en", "fr")

    assert provider.single_calls == 1
    assert service.get_provider_status()["available"] is False


def test_failed_batch_is_not_resent_line_by_line_to_the_same_provider():
    provider = FailingProvider()
    service = TranslationService(provider=provider)

    results = service.translate_batch_messages(["un", "deux", "trois"], "en", "fr")

    assert [success for _text, success, _name in results] == [False, False, False]
    assert provider.batch_calls == 1
    assert provider.single_calls == 0
    assert service.last_failure_reason == FAILURE_NETWORK


def test_discarded_response_reports_protected_name_change():
    provider = MagicMock()
    provider.name = "DeepL"
    provider.supports_batch = False
    provider.translate.side_effect = lambda text, *args, **kwargs: "Primary something"
    service = TranslationService(
        provider=provider,
        entity_index=ProtectedEntityIndex([ProtectedEntity("Raven", "ship")]),
    )

    assert service.translate_message("primary Raven", "en", "fr")[1] is False
    assert service.last_failure_reason == FAILURE_PROTECTED_NAMES


def test_deepl_sdk_retries_are_limited():
    sdk = MagicMock()
    sdk.http_client.max_network_retries = 5
    with patch.dict("sys.modules", {"deepl": sdk}):
        DeepLProvider("secret")

    assert sdk.http_client.max_network_retries == DEEPL_MAX_NETWORK_RETRIES


def test_stale_jobs_are_resolved_without_calling_the_provider():
    service = MagicMock()
    service.entity_index = None
    service.translate_message.return_value = ("hello", True, "DeepL")
    scheduler = TranslationScheduler(service)
    emitted = []
    scheduler.message_ready.connect(emitted.append)
    old = _job(
        "old",
        queued_at=time.monotonic() - MAX_TRANSLATION_JOB_AGE_SECONDS - 1,
    )

    scheduler._translate_jobs([old])
    scheduler._translate_jobs([_job("fresh")])

    assert [message.message_id for message in emitted] == ["old", "fresh"]
    assert emitted[0].state == TranslationState.FAILED
    assert emitted[0].error == STALE_TRANSLATION_ERROR
    assert emitted[1].state == TranslationState.TRANSLATED
    service.translate_message.assert_called_once()


def test_failed_line_shows_provider_reason():
    service = MagicMock()
    service.entity_index = None
    service.translate_message.return_value = ("bonjour", False, "DeepL")
    service.last_failure_reason = FAILURE_QUOTA
    scheduler = TranslationScheduler(service)
    emitted = []
    scheduler.message_ready.connect(emitted.append)

    scheduler._translate_jobs([_job()])

    assert emitted[-1].error == "DeepL: quota exhausted"


def test_scheduler_publishes_provider_status_when_availability_changes():
    service = MagicMock()
    service.entity_index = None
    service.translate_message.return_value = ("bonjour", False, "DeepL")
    service.get_provider_status.return_value = {
        "mode": "deepl",
        "name": "DeepL",
        "available": False,
        "reason": "DeepL paused: network error; retrying in 30s.",
    }
    scheduler = TranslationScheduler(service)
    statuses = []
    scheduler.provider_status_ready.connect(statuses.append)

    scheduler._translate_jobs([_job("a")])
    scheduler._translate_jobs([_job("b")])

    assert len(statuses) == 1
    assert statuses[0]["available"] is False
