from unittest.mock import patch

import pytest

from src.core.detector import LanguageDetector


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("EN-US", "en"),
        ("pt_BR", "pt"),
        ("zh-Hant-TW", "zh"),
        ("nb-NO", "no"),
        ("iw-IL", "he"),
        ("auto", "auto"),
        (None, "unknown"),
    ],
)
def test_normalize_language_variants(raw, expected):
    assert LanguageDetector.normalize_language_code(raw) == expected


@pytest.mark.parametrize(
    ("text", "target", "language"),
    [
        ("需要 shield 后勤", "zh-TW", "zh"),
        ("艦隊は jump の準備", "ja-JP", "ja"),
        ("함대 shield 준비", "ko-KR", "ko"),
    ],
)
def test_east_asian_message_already_in_target_is_not_translated(text, target, language):
    detector = LanguageDetector()

    assert detector.should_translate(text, target_lang=target, ignored_langs=set()) == (
        False,
        language,
    )


def test_mixed_chinese_command_is_not_suppressed_by_eve_keyword():
    detector = LanguageDetector()

    assert detector.should_translate(
        "需要 shield 后勤",
        target_lang="en-US",
        ignored_langs={"en"},
    ) == (True, "zh")


def test_mixed_cyrillic_command_is_not_suppressed_by_eve_keyword():
    with patch("src.core.detector.detect", return_value="ru"):
        detector = LanguageDetector()

        assert detector.should_translate(
            "Нужен shield для флота",
            target_lang="en",
            ignored_langs={"en"},
        ) == (True, "ru")


def test_force_ignore_keyword_must_match_the_complete_message():
    with patch("src.core.detector.detect", return_value="fr"):
        detector = LanguageDetector()

        for message in ("besoin shield maintenant", "Besoin Shield Maintenant"):
            assert detector.should_translate(
                message,
                target_lang="en",
                ignored_langs={"en"},
            ) == (True, "fr")
        assert detector.should_translate(
            "shield",
            target_lang="fr",
            ignored_langs=set(),
        ) == (False, "ignored_keyword")


@pytest.mark.parametrize(
    "message",
    [
        (
            "-HyperNet offer: Kronos HyperNet offer: Vargur "
            "HyperNet offer: Widow"
        ),
        "-HyperNet offer: PNI HyperNet offer: Odysseus",
        "-HyperNet512offer: Marshal HyperNetcheap8xoffer: Marshal",
        "cheap node! HyperNet offer: Marshal HyperNet offer: Marshal",
        "HyperNet offer: Revelation Navy Issue 11/16",
        "- Revelation Navy Issue Phoenix Navy Issue Moros Navy Issue "
        "HyperNet offer: Minokawa HyperNet offer: Revelation",
        "HyperNet: Salvation",
    ],
)
def test_generated_english_hypernet_offers_are_ignored(message):
    with patch("src.core.detector.detect") as mock_detect:
        detector = LanguageDetector()

        assert detector.should_translate(
            message,
            target_lang="de",
            ignored_langs=set(),
        ) == (False, "market_offer")
        mock_detect.assert_not_called()


@pytest.mark.parametrize(
    ("message", "detected"),
    [
        ("HyperNet offer: Kronos is overpriced", "en"),
        ("Can someone explain this HyperNet offer: Kronos?", "en"),
        ("HyperNet offers are ruining Jita", "en"),
        ("HyperNet offer: Kronos ist viel zu teuer", "de"),
        ("-HyperNet offer: totally fake thing", "en"),
    ],
)
def test_hypernet_discussion_remains_eligible_for_translation(message, detected):
    with patch("src.core.detector.detect", return_value=detected):
        detector = LanguageDetector()

        assert detector.should_translate(
            message,
            target_lang="fr",
            ignored_langs=set(),
        ) == (True, detected)


def test_non_latin_hypernet_discussion_keeps_script_priority():
    detector = LanguageDetector()

    assert detector.should_translate(
        "\u9700\u8981\u770b\u770b HyperNet offer: Kronos",
        target_lang="en",
        ignored_langs={"en"},
    ) == (True, "zh")


def test_general_language_variant_matching_target_is_not_translated():
    with patch("src.core.detector.detect", return_value="pt"):
        detector = LanguageDetector()

        assert detector.should_translate(
            "Esta mensagem ja esta em portugues",
            target_lang="pt-BR",
            ignored_langs=set(),
        ) == (False, "pt")


def test_ignored_language_variants_are_normalized():
    with patch("src.core.detector.detect", return_value="en"):
        detector = LanguageDetector()

        assert detector.should_translate(
            "This fleet is ready to jump",
            target_lang="fr",
            ignored_langs={"en-US"},
        ) == (False, "en")


@pytest.mark.parametrize("text", ["x", "3x", "o7", "111", "++++", "???"])
def test_short_ascii_and_symbol_messages_are_conservatively_skipped(text):
    with patch("src.core.detector.detect") as mock_detect:
        detector = LanguageDetector()

        should_translate, _ = detector.should_translate(
            text,
            target_lang="fr",
            ignored_langs=set(),
        )

        assert should_translate is False
        mock_detect.assert_not_called()


def test_short_ascii_detection_is_deterministic():
    assert LanguageDetector().detect_language("3x") == "en"
    assert LanguageDetector().detect_language("3x") == "en"


ENGLISH_FLEET_COMMANDS = (
    "align",
    "jump",
    "do not jump",
    "hold cloak",
    "primary is Drake",
)


@pytest.mark.parametrize("message", ["regroup", "star burst", "bubble up"])
def test_all_builtin_ascii_commands_use_combat_classifier_hint(message):
    with patch("src.core.detector.detect") as mock_detect:
        detector = LanguageDetector()

        assert detector.should_translate(
            message,
            target_lang="en",
            ignored_langs={"en"},
        ) == (False, "en")
        mock_detect.assert_not_called()


def test_upstream_doctrine_command_can_supply_an_explicit_source_hint():
    class Match:
        code = "CUSTOM"
        source_clause = "rojo ahora"

    with patch("src.core.detector.detect") as mock_detect:
        detector = LanguageDetector()

        assert detector.should_translate(
            "rojo ahora",
            target_lang="en",
            ignored_langs={"en"},
            command_matches=(Match(),),
            command_source_language="es-MX",
        ) == (True, "es")
        mock_detect.assert_not_called()


def test_doctrine_match_metadata_routes_a_short_command_without_detection():
    class Match:
        code = "CEASE_FIRE"
        source_clause = "red cycle"
        source_language = "en-US"

    with patch("src.core.detector.detect") as mock_detect:
        detector = LanguageDetector()

        assert detector.should_translate(
            "red cycle",
            target_lang="de",
            ignored_langs={"en"},
            command_matches=(Match(),),
        ) == (True, "en")
        mock_detect.assert_not_called()


def test_partial_command_classification_does_not_override_mixed_line_detection():
    with patch("src.core.detector.detect", return_value="fr"):
        detector = LanguageDetector()

        assert detector.should_translate(
            "jump; bonjour tout le monde",
            target_lang="en",
            ignored_langs={"en"},
        ) == (True, "fr")


@pytest.mark.parametrize("message", ENGLISH_FLEET_COMMANDS)
def test_english_fleet_commands_do_not_make_remote_calls_for_english_target(message):
    detector = LanguageDetector()

    assert detector.should_translate(
        message,
        target_lang="en-US",
        ignored_langs={"en"},
    ) == (False, "en")


@pytest.mark.parametrize("message", ENGLISH_FLEET_COMMANDS)
def test_english_fleet_commands_still_translate_for_non_english_target(message):
    detector = LanguageDetector()

    assert detector.should_translate(
        message,
        target_lang="de",
        ignored_langs={"en"},
    ) == (True, "en")


@pytest.mark.parametrize(
    ("message", "detected"),
    [
        ("alinear con la puerta", "es"),
        ("align con la puerta", "es"),
        ("sautez maintenant", "fr"),
        ("la cible principale est Drake", "fr"),
    ],
)
def test_foreign_fleet_commands_are_not_mistaken_for_english_patterns(message, detected):
    with patch("src.core.detector.detect", return_value=detected):
        detector = LanguageDetector()

        assert detector.should_translate(
            message,
            target_lang="en",
            ignored_langs={"en"},
        ) == (True, detected)


@pytest.mark.parametrize(
    "message",
    [
        "GATE RED",
        "dock up",
        "good luck",
        "need dps",
        "lets kill it",
        "never forget your bottle!",
        "Nova Rage Heavy Assault Missile",
        "Otsasai",
    ],
)
def test_plain_english_chat_and_names_are_not_sent_for_english_target(message):
    detector = LanguageDetector()

    assert detector.should_translate(
        message,
        target_lang="en",
        ignored_langs={"en"},
    ) == (False, "en")


@pytest.mark.parametrize("message", ["4-HWWF", "15W-GC*", "J123456 x3"])
def test_system_codes_without_words_are_short_text(message):
    detector = LanguageDetector()

    assert detector.should_translate(
        message,
        target_lang="en",
        ignored_langs={"en"},
    ) == (False, "short_text")


def test_plain_english_chat_still_translates_for_non_english_target():
    detector = LanguageDetector()

    assert detector.should_translate(
        "need dps",
        target_lang="zh",
        ignored_langs=set(),
    ) == (True, "en")


@pytest.mark.parametrize(
    ("message", "detected"),
    [
        ("necesito ayuda en el gate ahora", "es"),
        ("wir brauchen hilfe am gate", "de"),
        ("merci beaucoup pour le fleet", "fr"),
    ],
)
def test_foreign_ascii_chat_with_few_english_words_uses_statistical_detection(
    message,
    detected,
):
    detector = LanguageDetector()

    assert detector.should_translate(
        message,
        target_lang="en",
        ignored_langs={"en"},
    ) == (True, detected)
