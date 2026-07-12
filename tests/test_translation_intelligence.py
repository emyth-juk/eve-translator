from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.entity_protection import (
    LocalizedEntity,
    ProtectedEntity,
    ProtectedEntityIndex,
)
from src.core.translation_memory import TranslationMemory
from src.services.translator import (
    DeepLProvider,
    GLOSSARY_CACHE_SIZE,
    GoogleTransProvider,
    LEXICON_MATCHER_CACHE_SIZE,
    TranslationService,
)


class RecordingGlossary:
    def __init__(self, source_lang, target_lang):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.terms = {
            text: f"{self.source_lang}>{self.target_lang}:{text}"
            for text in ("bonjour", "salut", "hallo")
        }


class RecordingGlossaryFactory:
    def __init__(self):
        self.calls = []

    def __call__(self, source_lang, target_lang):
        self.calls.append((source_lang, target_lang))
        return RecordingGlossary(source_lang, target_lang)


class SingleProvider:
    name = "Single"
    supports_batch = False
    supports_context = False

    def __init__(self):
        self.calls = []

    def translate(self, text, target_lang='en', source_lang=None):
        self.calls.append((text, target_lang, source_lang))
        return f"translated:{text}"


class BatchProvider:
    name = "Batch"
    supports_batch = True
    supports_context = True

    def __init__(self, response=None):
        self.response = response
        self.batch_calls = []
        self.single_calls = []

    def translate_batch(
        self,
        texts,
        target_lang='en',
        source_lang=None,
        context=None,
    ):
        self.batch_calls.append((list(texts), target_lang, source_lang, context))
        if self.response is not None:
            return self.response
        return [f"batch:{text}" for text in texts]

    def translate(
        self,
        text,
        target_lang='en',
        source_lang=None,
        context=None,
    ):
        self.single_calls.append((text, target_lang, source_lang, context))
        return f"single:{text}"


class PlaceholderManglerProvider(SingleProvider):
    def __init__(self, transform):
        super().__init__()
        self.transform = transform

    def translate(self, text, target_lang='en', source_lang=None):
        self.calls.append((text, target_lang, source_lang))
        return self.transform(text)


def test_glossary_is_selected_lazily_for_detected_source_and_target():
    factory = RecordingGlossaryFactory()
    provider = SingleProvider()
    service = TranslationService(provider=provider, glossary_factory=factory)

    first = service.translate_message("bonjour", "de", "fr")
    second = service.translate_message("salut", "de", "fr")
    service.translate_message("hallo", "en", "de")

    assert first == ("translated:fr>de:bonjour", True, "Single")
    assert second == ("translated:fr>de:salut", True, "Single")
    assert factory.calls == [("zh", "en"), ("fr", "de"), ("de", "en")]
    assert service.glossary.source_lang == "de"
    assert service.glossary.target_lang == "en"


def test_regional_detected_language_uses_base_pair_glossary():
    factory = RecordingGlossaryFactory()
    service = TranslationService(provider=SingleProvider(), glossary_factory=factory)

    service.translate_message("舰队", "en", "zh-cn")

    assert factory.calls == [("zh", "en")]
    assert service.glossary.source_lang == "zh"


def test_regional_target_language_is_preserved_for_layered_glossary_loading():
    factory = RecordingGlossaryFactory()
    service = TranslationService(provider=SingleProvider(), glossary_factory=factory)

    service.translate_message("舰队", "de-DE", "zh-cn")

    assert factory.calls == [("zh", "en"), ("zh", "de-de")]
    assert service.glossary.target_lang == "de-de"


def test_glossary_cache_is_bounded_and_refreshes_recent_pairs():
    service = TranslationService(
        provider=SingleProvider(),
        glossary_factory=RecordingGlossaryFactory(),
    )

    for number in range(GLOSSARY_CACHE_SIZE - 1):
        service._get_glossary(f"l{number}", "en")
    service._get_glossary("zh", "en")  # Refresh the initial pair.
    service._get_glossary(f"l{GLOSSARY_CACHE_SIZE}", "en")

    assert len(service._glossaries) == GLOSSARY_CACHE_SIZE
    assert ("zh", "en") in service._glossaries
    assert ("l0", "en") not in service._glossaries


def test_lexicon_matcher_cache_is_bounded_and_refreshes_recent_contexts():
    service = TranslationService(
        provider=SingleProvider(),
        glossary_factory=RecordingGlossaryFactory(),
    )
    glossary = service._get_glossary("fr", "en")
    keys = []
    for number in range(LEXICON_MATCHER_CACHE_SIZE):
        service.doctrine_pack = SimpleNamespace(
            scope="fleet",
            revision=str(number),
        )
        keys.append(service._lexicon_context("fr", "en", glossary))
        service._lexicon_matcher("fr", "en", glossary, {})

    service.doctrine_pack = SimpleNamespace(scope="fleet", revision="0")
    service._lexicon_matcher("fr", "en", glossary, {})
    service.doctrine_pack = SimpleNamespace(
        scope="fleet",
        revision=str(LEXICON_MATCHER_CACHE_SIZE),
    )
    service._lexicon_matcher("fr", "en", glossary, {})

    assert len(service._lexicon_matchers) == LEXICON_MATCHER_CACHE_SIZE
    assert keys[0] in service._lexicon_matchers
    assert keys[1] not in service._lexicon_matchers


def test_exact_local_correction_bypasses_disabled_or_cloud_provider(tmp_path):
    memory = TranslationMemory(tmp_path / "memory.sqlite3")
    memory.remember("Ne sautez pas!", "Do not jump!", "fr", "en")
    provider = SingleProvider()
    service = TranslationService(provider=provider, translation_memory=memory)

    result = service.translate_message("Ne sautez pas!", "en", "fr")

    assert result == ("Do not jump!", True, "Local correction")
    assert provider.calls == []


def test_scoped_correction_wins_then_falls_back_to_global(tmp_path):
    class Doctrine:
        scope = "armor@1"
        revision = "armor@1:abc"
        protected_names = ()

        @staticmethod
        def terms_for(_source, _target):
            return {}

    memory = TranslationMemory(tmp_path / "memory.sqlite3")
    memory.remember("hold", "Global hold", "fr", "en")
    memory.remember("jump", "Doctrine jump", "fr", "en", "armor@1")
    service = TranslationService(
        provider=SingleProvider(),
        translation_memory=memory,
        doctrine_pack=Doctrine(),
    )

    assert service.translate_message("jump", "en", "fr") == (
        "Doctrine jump", True, "Local correction"
    )
    assert service.translate_message("hold", "en", "fr") == (
        "Global hold", True, "Local correction"
    )


def test_active_doctrine_exact_correction_applies_without_importing_to_memory():
    class Doctrine:
        scope = "armor@2"
        revision = "armor@2:abc"
        protected_names = ()

        @staticmethod
        def terms_for(_source, _target):
            return {}

        @staticmethod
        def correction_for(text, source, target):
            if (text, source, target) == ("Ne sautez pas!", "fr", "en"):
                return "Do not jump!"
            return None

    provider = SingleProvider()
    service = TranslationService(provider=provider, doctrine_pack=Doctrine())

    assert service.translate_message("Ne sautez pas!", "en", "fr") == (
        "Do not jump!", True, "Local correction"
    )
    assert provider.calls == []


def test_entity_index_is_protected_from_provider_and_restored():
    provider = SingleProvider()
    entity_index = ProtectedEntityIndex([
        ProtectedEntity("Raven Navy Issue", "ship"),
    ])
    service = TranslationService(provider=provider, entity_index=entity_index)

    result = service.translate_message("Primary Raven Navy Issue", "fr", "en")

    assert "Raven Navy Issue" not in provider.calls[0][0]
    assert "__EVEENTITY_1__" in provider.calls[0][0]
    assert result == ("translated:Primary Raven Navy Issue", True, "Single")


def test_entity_placeholder_restores_after_provider_lowercases_it():
    provider = PlaceholderManglerProvider(str.lower)
    entity_index = ProtectedEntityIndex([ProtectedEntity("Raven", "ship")])
    service = TranslationService(provider=provider, entity_index=entity_index)

    assert service.translate_message("Primary Raven", "fr", "en") == (
        "primary Raven", True, "Single"
    )


def test_doctrine_placeholder_restores_with_spaces_around_separators():
    class Doctrine:
        scope = "shield@1"
        revision = "shield@1:one"
        protected_names = ("Night Raven",)

        @staticmethod
        def terms_for(_source, _target):
            return {}

    def add_spaces(text):
        return text.replace(
            "__EVEDOCTRINE_1__", "_ _  evedoctrine _ 1 _ _"
        )

    service = TranslationService(
        provider=PlaceholderManglerProvider(add_spaces),
        doctrine_pack=Doctrine(),
    )

    assert service.translate_message("Primary Night Raven", "fr", "en") == (
        "Primary Night Raven", True, "Single"
    )


def test_placeholder_restoration_preserves_surrounding_whitespace():
    restored = TranslationService._restore_protected_entities(
        "Primary _ _ eveentity _ 1 _ _  now",
        {"__EVEENTITY_1__": "Raven"},
    )

    assert restored == "Primary Raven  now"


def test_placeholder_restoration_recovers_only_reserved_identity_mutations():
    variants = (
        "Primary EVEENTITY_1 now",
        "Primary eveentity1 now",
        "Primary _EVEENTITY-1_ now",
        "Primary \uff3fEVEENTITY\uff3f1\uff3f now",
    )

    for translated in variants:
        assert TranslationService._restore_protected_entities(
            translated,
            {"__EVEENTITY_1__": "Raven"},
        ) == "Primary Raven now"


def test_placeholder_restoration_rejects_mixed_duplicate_variants():
    restored = TranslationService._restore_protected_entities(
        "__EVEENTITY_1__ and _ _ eveentity _ 1 _ _",
        {"__EVEENTITY_1__": "Raven"},
    )

    assert restored is None


def test_placeholder_restoration_rejects_word_embedded_variant():
    restored = TranslationService._restore_protected_entities(
        "x__eveentity_1__y",
        {"__EVEENTITY_1__": "Raven"},
    )

    assert restored is None


def test_missing_or_duplicated_placeholder_fails_closed_to_original():
    entity_index = ProtectedEntityIndex([ProtectedEntity("Raven", "ship")])
    missing = TranslationService(
        provider=PlaceholderManglerProvider(
            lambda text: text.replace("__EVEENTITY_1__", "")
        ),
        entity_index=entity_index,
    )
    duplicated = TranslationService(
        provider=PlaceholderManglerProvider(
            lambda text: text.replace(
                "__EVEENTITY_1__", "__EVEENTITY_1__ __EVEENTITY_1__"
            )
        ),
        entity_index=entity_index,
    )

    assert missing.translate_message("Primary Raven", "fr", "en") == (
        "Primary Raven", False, "Single"
    )
    assert duplicated.translate_message("Primary Raven", "fr", "en") == (
        "Primary Raven", False, "Single"
    )


def test_one_missing_marker_rejects_the_entire_partially_restorable_response():
    restored = TranslationService._restore_protected_entities(
        "Primary EVEENTITY_1 and the second protected name disappeared",
        {
            "__EVEENTITY_1__": "Raven",
            "__EVEENTITY_2__": "Jita",
        },
    )

    # Returning a partly restored provider response could silently corrupt an
    # FC call. The translation pipeline treats None as a full fallback to the
    # untouched original message.
    assert restored is None


def test_same_base_language_uses_local_correction_then_skips_provider():
    class Corrections:
        calls = []

        @classmethod
        def lookup(cls, message, source, target, scope):
            cls.calls.append((message, source, target, scope))
            return "Corrected formup" if message == "formup" else None

    provider = SingleProvider()
    service = TranslationService(provider=provider, translation_memory=Corrections())

    assert service.translate_message("formup", "en-US", "en") == (
        "Corrected formup", True, "Local correction"
    )
    assert service.translate_message("already English", "en-US", "en-GB") == (
        "already English", True, "Local"
    )
    assert provider.calls == []
    assert Corrections.calls == [
        ("formup", "en", "en-us", ""),
        ("already English", "en-gb", "en-us", ""),
    ]


def test_official_sde_names_canonicalize_before_yaml_ship_aliases():
    provider = SingleProvider()
    entity_index = ProtectedEntityIndex(
        [
            ProtectedEntity("Apocalypse", "ship"),
            ProtectedEntity("Astrahus", "structure"),
            ProtectedEntity("Fortizar", "structure"),
            ProtectedEntity("Keepstar", "structure"),
            ProtectedEntity("灾难级", "ship", strict_case=False),
            ProtectedEntity("空堡", "structure", strict_case=False),
            ProtectedEntity("铁壁", "structure", strict_case=False),
            ProtectedEntity("星城", "structure", strict_case=False),
        ],
        localized_entities=[
            LocalizedEntity(1, "ship", {"en": "Apocalypse", "zh": "灾难级"}),
            LocalizedEntity(2, "structure", {"en": "Astrahus", "zh": "空堡"}),
            LocalizedEntity(3, "structure", {"en": "Fortizar", "zh": "铁壁"}),
            LocalizedEntity(4, "structure", {"en": "Keepstar", "zh": "星城"}),
        ],
    )
    service = TranslationService(provider=provider, entity_index=entity_index)

    result = service.translate_message("灾难级 灾难 空堡 铁壁 星城", "en", "zh")

    request = provider.calls[0][0]
    assert "级" not in request
    assert "灾难" not in request
    assert result == (
        "translated:Apocalypse Apocalypse Astrahus Fortizar Keepstar",
        True,
        "Single",
    )


def test_exact_custom_glossary_can_override_equal_length_sde_name():
    class CustomGlossary:
        source_lang = "zh"
        target_lang = "en"
        terms = {"空堡": "Alliance House"}

    index = ProtectedEntityIndex(
        [ProtectedEntity("Astrahus", "structure")],
        localized_entities=[LocalizedEntity(
            35832, "structure", {"en": "Astrahus", "zh": "空堡"}
        )],
    )
    service = TranslationService(
        provider=SingleProvider(),
        entity_index=index,
        glossary_factory=lambda **_kwargs: CustomGlossary(),
    )

    assert service.translate_message("空堡", "en", "zh") == (
        "translated:Alliance House",
        True,
        "Single",
    )


def test_official_ambiguous_span_beats_shorter_doctrine_alias():
    class Doctrine:
        scope = "raven@1"
        revision = "raven@1:abc"
        protected_names = ()

        @staticmethod
        def terms_for(source, target):
            assert (source, target) == ("en", "fr")
            return {"Raven": "Corvus"}

    provider = SingleProvider()
    index = ProtectedEntityIndex(
        [ProtectedEntity("Raven Navy Issue", "ship")],
        localized_entities=[LocalizedEntity(
            1,
            "ship",
            {"en": "Raven Navy Issue", "fr": "Raven Navy Issue"},
        ), LocalizedEntity(
            2,
            "ship",
            {"en": "Raven Navy Issue", "fr": "Corbeau Navy Issue"},
        )],
    )
    service = TranslationService(
        provider=provider,
        entity_index=index,
        doctrine_pack=Doctrine(),
    )

    assert service.translate_message("Primary Raven Navy Issue", "fr", "en") == (
        "translated:Primary Raven Navy Issue",
        True,
        "Single",
    )
    assert "Corvus" not in provider.calls[0][0]


def test_cache_key_keeps_different_entities_distinct_after_tokenization():
    provider = SingleProvider()
    entity_index = ProtectedEntityIndex([
        ProtectedEntity("Raven", "ship"),
        ProtectedEntity("Jita", "solar_system"),
    ])
    service = TranslationService(provider=provider, entity_index=entity_index)

    raven = service.translate_message("Primary Raven", "fr", "en")
    jita = service.translate_message("Primary Jita", "fr", "en")

    assert raven == ("translated:Primary Raven", True, "Single")
    assert jita == ("translated:Primary Jita", True, "Single")
    assert len(provider.calls) == 2


def test_warm_cache_hit_bypasses_entity_and_lexicon_preprocessing():
    class CountingEntityIndex:
        available = True
        entity_count = 1
        metadata = {"asset_sha256": "stable-sde-revision"}

        def __init__(self):
            self.lexicon_calls = 0
            self.protect_calls = 0

        def lexicon_terms_for(self, _source, _target):
            self.lexicon_calls += 1
            return {"Raven": "Raven"}

        def protect(self, text):
            self.protect_calls += 1
            return SimpleNamespace(text=text, tokens={})

    provider = SingleProvider()
    entity_index = CountingEntityIndex()
    service = TranslationService(provider=provider, entity_index=entity_index)

    first = service.translate_message("Primary Raven", "fr", "en")
    second = service.translate_message("Primary Raven", "fr", "en")

    assert first == second == ("translated:Primary Raven", True, "Single")
    assert len(provider.calls) == 1
    assert entity_index.lexicon_calls == 1
    assert entity_index.protect_calls == 1


def test_doctrine_terms_precede_glossary_and_protected_names_are_restored():
    class Doctrine:
        scope = "shield@2"
        revision = "shield@2:def"
        protected_names = ("Night Raven",)

        @staticmethod
        def terms_for(source, target):
            assert (source, target) == ("fr", "en")
            return {"cible": "PRIMARY"}

    provider = SingleProvider()
    service = TranslationService(provider=provider, doctrine_pack=Doctrine())

    result = service.translate_message("cible Night Raven", "en", "fr")

    request = provider.calls[0][0]
    assert request.startswith("PRIMARY ")
    assert "Night Raven" not in request
    assert result == ("translated:PRIMARY Night Raven", True, "Single")


def test_regional_language_uses_base_doctrine_pair_when_exact_pair_is_empty():
    class Doctrine:
        scope = "armor@1"
        revision = "armor@1:abc"
        protected_names = ()

        @staticmethod
        def terms_for(source, target):
            return {"守门": "HOLD"} if (source, target) == ("zh", "en") else {}

    provider = SingleProvider()
    service = TranslationService(provider=provider, doctrine_pack=Doctrine())

    service.translate_message("守门", "en-US", "zh-Hans")

    assert provider.calls[0][0] == "HOLD"


def test_batch_uses_one_provider_request_and_restores_each_entity():
    provider = BatchProvider()
    entity_index = ProtectedEntityIndex([
        ProtectedEntity("Raven", "ship"),
        ProtectedEntity("Jita", "solar_system"),
    ])
    service = TranslationService(provider=provider, entity_index=entity_index)

    results = service.translate_batch_messages(
        ["Primary Raven", "Warp Jita"],
        "fr",
        "en",
        context="FC said hold",
    )

    assert results == [
        ("batch:Primary Raven", True, "Batch"),
        ("batch:Warp Jita", True, "Batch"),
    ]
    assert len(provider.batch_calls) == 1
    sent, target, source, context = provider.batch_calls[0]
    assert sent == ["Primary __EVEENTITY_1__", "Warp __EVEENTITY_1__"]
    assert (target, source, context) == ("fr", "en", "FC said hold")
    assert provider.single_calls == []


def test_batch_skips_provider_when_source_and_target_share_base_language():
    provider = BatchProvider()
    service = TranslationService(provider=provider)

    assert service.translate_batch_messages(
        ["Bom dia", "Alinhar no portao"],
        "pt-BR",
        "pt",
    ) == [
        ("Bom dia", True, "Local"),
        ("Alinhar no portao", True, "Local"),
    ]
    assert provider.batch_calls == []
    assert provider.single_calls == []


def test_context_is_not_passed_to_provider_without_capability():
    provider = SingleProvider()
    service = TranslationService(provider=provider)

    service.translate_message("bonjour", "en", "fr", context="private history")

    assert provider.calls == [("bonjour", "en", "fr")]


def test_partial_batch_failure_falls_back_only_for_failed_item_in_order():
    provider = BatchProvider(response=["batch:first", None, "batch:third"])
    service = TranslationService(provider=provider)

    results = service.translate_batch_messages(
        ["first", "second", "third"], "en", "fr", context="prior"
    )

    assert results == [
        ("batch:first", True, "Batch"),
        ("single:second", True, "Batch"),
        ("batch:third", True, "Batch"),
    ]
    assert provider.single_calls == [("second", "en", "fr", "prior")]


def test_consented_provider_fallback_handles_only_failed_batch_items():
    primary = BatchProvider(response=["batch:first", None, "batch:third"])
    primary.name = "DeepL"
    fallback = SingleProvider()
    fallback.name = "Google"
    service = TranslationService(provider_factories={
        'deepl': lambda _key: primary,
        'google': lambda _key: fallback,
    })
    service.set_config({
        'translation_provider': 'deepl',
        'provider_fallback_enabled': True,
        'deepl_api_key': 'consented-key',
        'target_language': 'en',
    })

    results = service.translate_batch_messages(
        ["first", "second", "third"], "en", "fr", context="prior"
    )

    assert results == [
        ("batch:first", True, "DeepL"),
        ("translated:second", True, "Google"),
        ("batch:third", True, "DeepL"),
    ]
    assert primary.single_calls == []
    assert fallback.calls == [("second", "en", "fr")]


def test_wrong_batch_result_count_falls_back_all_without_reordering():
    provider = BatchProvider(response=["only one"])
    service = TranslationService(provider=provider)

    results = service.translate_batch_messages(["one", "two", "three"], "en", "fr")

    assert results == [
        ("single:one", True, "Batch"),
        ("single:two", True, "Batch"),
        ("single:three", True, "Batch"),
    ]
    assert [call[0] for call in provider.single_calls] == ["one", "two", "three"]


def test_deepl_provider_sends_one_list_request_with_context():
    sdk = MagicMock()
    translator = sdk.Translator.return_value
    translator.translate_text.return_value = [
        SimpleNamespace(text="one translated"),
        SimpleNamespace(text="two translated"),
    ]
    with patch.dict('sys.modules', {'deepl': sdk}):
        provider = DeepLProvider("secret")
        result = provider.translate_batch(
            ["one", "two"], "en", "fr", context="previous fleet line"
        )

    assert provider.supports_batch is True
    assert provider.supports_context is True
    assert result == ["one translated", "two translated"]
    translator.translate_text.assert_called_once_with(
        ["one", "two"],
        target_lang="EN-US",
        source_lang="FR",
        context="previous fleet line",
    )


def test_google_does_not_claim_loop_batching():
    with patch('src.services.translator.GoogleTranslator'):
        provider = GoogleTransProvider()

    assert provider.supports_batch is False
    assert provider.supports_context is False
