from src.core.lexicon import LexiconTerm, LongestLexiconMatcher


def test_longest_span_beats_shorter_higher_priority_alias():
    matcher = LongestLexiconMatcher([
        LexiconTerm("灾难级", "Apocalypse", priority=0),
        LexiconTerm("灾难", "Apocalypse", priority=20),
    ])

    assert matcher.replace("集火灾难级 然后灾难") == (
        "集火 Apocalypse 然后 Apocalypse"
    )


def test_higher_priority_wins_only_for_equal_source_span():
    matcher = LongestLexiconMatcher([
        LexiconTerm("空堡", "Astrahus", priority=0),
        LexiconTerm("空堡", "Custom Citadel", priority=20),
    ])

    assert matcher.replace("空堡") == "Custom Citadel"


def test_identity_sde_span_guards_against_shorter_alias_replacement():
    matcher = LongestLexiconMatcher([
        LexiconTerm("Raven Navy Issue", "Raven Navy Issue", priority=0),
        LexiconTerm("Raven", "Corvus", priority=20),
    ])

    assert matcher.replace("Primary Raven Navy Issue") == (
        "Primary Raven Navy Issue"
    )
    assert matcher.replace("Raven Navy Issue, Raven Navy Issue's") == (
        "Raven Navy Issue, Raven Navy Issue's"
    )


def test_latin_alias_requires_word_boundaries_and_preserves_case():
    matcher = LongestLexiconMatcher([LexiconTerm("Geddon", "Armageddon", 10)])

    assert matcher.replace("Primary Geddon") == "Primary Armageddon"
    assert matcher.replace("MegaGeddonFit") == "MegaGeddonFit"
    assert matcher.replace("geddon") == "geddon"


def test_cjk_alias_does_not_require_latin_word_boundaries():
    matcher = LongestLexiconMatcher([LexiconTerm("\u7a7a\u5821", "Astrahus", 10)])

    assert matcher.replace("A\u7a7a\u5821B") == "A Astrahus B"


def test_no_terms_leaves_whitespace_untouched():
    assert LongestLexiconMatcher().replace("keep   spacing") == "keep   spacing"


def test_single_cjk_character_term_only_matches_when_standalone():
    matcher = LongestLexiconMatcher([
        LexiconTerm("收", "WTB", 10),  # 收
        LexiconTerm("跳", "Jump", 10),  # 跳
    ])

    # 收到 ("roger") and 跳跃 are ordinary words, not the WTB/Jump shorthand.
    assert matcher.replace("收到") == "收到"
    assert matcher.replace("准备跳跃") == "准备跳跃"
    assert matcher.replace("收 PLEX") == "WTB PLEX"
    assert matcher.replace("收PLEX") == "WTB PLEX"
    assert matcher.replace("跳！") == "Jump ！"


def test_latin_alias_matches_next_to_cjk_text():
    matcher = LongestLexiconMatcher([LexiconTerm("YST", "Ishtar", 10)])

    assert matcher.replace("YST来了") == "Ishtar 来了"
    assert matcher.replace("打YST") == "打 Ishtar"
    # Latin neighbours still make the alias part of another word.
    assert matcher.replace("XYST") == "XYST"
