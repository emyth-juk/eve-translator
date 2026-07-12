from src.core.combat import (
    classify_commands,
    command_is_trusted,
    normalize_speaker_name,
    normalize_speaker_roles,
)


def test_negated_jump_wins_over_jump():
    matches = classify_commands("Do not jump!")
    assert [match.code for match in matches] == ["DO_NOT_JUMP"]


def test_complete_clause_matching_avoids_dangerous_substrings():
    assert classify_commands("I am jumping now") == ()
    assert classify_commands("the primary school is nearby") == ()
    assert classify_commands("Holden is primary") == ()


def test_arguments_and_multiple_clauses_are_preserved():
    matches = classify_commands("Align X47L-Q; primary Drake; prop on; jump")
    assert [(match.code, match.argument) for match in matches] == [
        ("ALIGN", "X47L-Q"),
        ("PRIMARY", "Drake"),
        ("PROP_ON", ""),
    ]


def test_chinese_exact_commands_are_recognized_from_original():
    matches = classify_commands("不要过门；集火幼龙")
    assert [(match.code, match.argument) for match in matches] == [
        ("DO_NOT_JUMP", ""),
        ("PRIMARY", "幼龙"),
    ]


def test_speaker_roles_are_explicit_normalized_and_system_is_rejected():
    roles = normalize_speaker_roles({
        "  Fleet  Boss ": "FC",
        "EVE System": "fc",
        "Scout One": "scout",
        "Bad": "admin",
    })
    assert roles == {"fleet boss": "fc", "scout one": "scout"}
    assert normalize_speaker_name("Ｆleet   Boss") == "fleet boss"


def test_only_fc_execution_commands_are_trusted():
    assert command_is_trusted("fc", "JUMP") is True
    assert command_is_trusted("scout", "JUMP") is False
    assert command_is_trusted("scout", "HOLD", "HOLD") is False
    assert command_is_trusted("scout", "DO_NOT_JUMP", "HOLD") is False
    assert command_is_trusted("starred", "JUMP") is False


def test_doctrine_can_add_only_an_exact_existing_command_code():
    class Pack:
        def command_for_phrase(self, phrase):
            return "CEASE_FIRE" if phrase.casefold() == "red cycle" else None

        @staticmethod
        def command_source_language(code):
            return "en" if code == "CEASE_FIRE" else ""

    matches = classify_commands("red cycle", doctrine_pack=Pack())

    assert [(item.code, item.source_language) for item in matches] == [
        ("CEASE_FIRE", "en"),
    ]
    assert classify_commands("red cycles", doctrine_pack=Pack()) == ()


def test_doctrine_custom_command_uses_validated_category_and_label():
    class Pack:
        def command_for_phrase(self, phrase):
            return "CHECK_DSCAN" if phrase.casefold() == "eyes on dscan" else None

        @staticmethod
        def command_category(code):
            return "INTEL" if code == "CHECK_DSCAN" else None

        @staticmethod
        def command_label(code):
            return "Check directional scan" if code == "CHECK_DSCAN" else code

    match, = classify_commands("eyes on dscan", doctrine_pack=Pack())

    assert (match.code, match.category, match.display_label) == (
        "CHECK_DSCAN", "INTEL", "Check directional scan"
    )
    assert command_is_trusted("scout", match.code, match.category) is False
    assert command_is_trusted("scout", "CUSTOM_MOVE", "MOVE") is False
