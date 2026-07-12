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
    assert command_is_trusted("starred", "JUMP") is False


def test_doctrine_can_add_only_an_exact_existing_command_code():
    class Pack:
        def command_for_phrase(self, phrase):
            return "CEASE_FIRE" if phrase.casefold() == "red cycle" else None

    assert [item.code for item in classify_commands(
        "red cycle", doctrine_pack=Pack()
    )] == ["CEASE_FIRE"]
    assert classify_commands("red cycles", doctrine_pack=Pack()) == ()
