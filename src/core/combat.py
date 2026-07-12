"""Deterministic fleet-command recognition and speaker-role helpers.

Combat cards are intentionally derived from the original chat line, never from
provider output.  That keeps a bad or delayed translation from turning ordinary
chat into an authoritative fleet command.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


ROLE_FC = "fc"
ROLE_SCOUT = "scout"
ROLE_STARRED = "starred"
SPEAKER_ROLES = frozenset({ROLE_FC, ROLE_SCOUT, ROLE_STARRED})

COMMAND_CATEGORY = {
    "HOLD": "HOLD",
    "DO_NOT_JUMP": "HOLD",
    "CEASE_FIRE": "HOLD",
    "PROP_OFF": "HOLD",
    "BUBBLE_DOWN": "HOLD",
    "JUMP": "MOVE",
    "ALIGN": "MOVE",
    "WARP": "MOVE",
    "ANCHOR": "MOVE",
    "FREE_BURN": "MOVE",
    "REGROUP": "MOVE",
    "STAR_BURST": "MOVE",
    "PRIMARY": "TARGET",
    "SWITCH_PRIMARY": "TARGET",
    "LOCK": "TARGET",
    "PROP_ON": "SUPPORT",
    "OVERHEAT": "SUPPORT",
    "BUBBLE_UP": "SUPPORT",
}

EXECUTION_COMMANDS = frozenset({
    "JUMP", "ALIGN", "WARP", "ANCHOR", "FREE_BURN", "PRIMARY",
    "SWITCH_PRIMARY", "LOCK", "CEASE_FIRE", "PROP_ON", "PROP_OFF",
    "OVERHEAT", "BUBBLE_UP", "BUBBLE_DOWN", "REGROUP", "STAR_BURST",
})


@dataclass(frozen=True)
class CommandMatch:
    """One complete, grammar-matched fleet command clause."""

    code: str
    category: str
    argument: str = ""
    source_clause: str = ""


def normalize_speaker_name(name: str) -> str:
    """Return a stable exact-match key without changing displayed spelling."""

    normalized = unicodedata.normalize("NFKC", name or "")
    return " ".join(normalized.split()).casefold()


def normalize_speaker_roles(value: object) -> dict[str, str]:
    """Validate persisted speaker roles and discard unsafe/system entries."""

    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for name, role in value.items():
        if not isinstance(name, str) or not isinstance(role, str):
            continue
        normalized_name = normalize_speaker_name(name)
        normalized_role = role.strip().lower()
        if (
            normalized_name
            and normalized_name not in {"eve system", "system"}
            and normalized_role in SPEAKER_ROLES
        ):
            result[normalized_name] = normalized_role
    return result


def speaker_role(name: str, roles: object) -> str:
    return normalize_speaker_roles(roles).get(normalize_speaker_name(name), "")


def command_is_trusted(role: str, command_code: str) -> bool:
    """FC calls are trusted; scouts may only create non-execution intel cards."""

    normalized_role = (role or "").strip().lower()
    if normalized_role == ROLE_FC:
        return True
    if normalized_role == ROLE_SCOUT:
        return command_code not in EXECUTION_COMMANDS
    return False


_ARGUMENT = r"(?P<argument>[\w][\w .'+/\-]{0,80}?)"


def _rules() -> Iterable[tuple[str, str]]:
    # Negatives must be evaluated before their positive counterparts.
    return (
        ("DO_NOT_JUMP", r"(?:do\s+not|don['’]?t|no)\s+jump(?:\s+yet)?"),
        ("DO_NOT_JUMP", r"(?:不要|别|禁止)(?:过门|跳|跃迁)"),
        ("HOLD", r"hold(?:\s+(?:cloak|gate|position|on))?"),
        ("HOLD", r"(?:保持|原地|等一下|不要动|别动)(?:隐身|位置)?"),
        ("CEASE_FIRE", r"(?:cease|hold|stop)\s+fire"),
        ("CEASE_FIRE", r"(?:停火|停止开火)"),
        ("PROP_OFF", r"(?:prop|mwd|ab)\s+off"),
        ("PROP_OFF", r"(?:关推|关闭推进|推进关闭)"),
        ("BUBBLE_DOWN", r"(?:bubble|bubbles)\s+down"),
        ("BUBBLE_DOWN", r"(?:收泡泡|泡泡收起)"),
        ("SWITCH_PRIMARY", rf"(?:switch|change)\s+(?:primary|dps)\s+(?:to\s+)?{_ARGUMENT}"),
        ("SWITCH_PRIMARY", rf"(?:转火|切换目标)\s*{_ARGUMENT}"),
        ("PRIMARY", rf"primary(?:\s+is)?\s+{_ARGUMENT}"),
        ("PRIMARY", rf"(?:集火|主目标)\s*{_ARGUMENT}"),
        ("ALIGN", rf"align(?:\s+(?:to|on))?\s+{_ARGUMENT}"),
        ("ALIGN", rf"(?:对齐|朝向)\s*{_ARGUMENT}"),
        ("ALIGN", r"align"),
        ("ALIGN", r"(?:对齐|朝向)"),
        ("WARP", rf"warp(?:\s+(?:to|on))?\s+{_ARGUMENT}"),
        ("WARP", rf"(?:跃迁|起跳)(?:到)?\s*{_ARGUMENT}"),
        ("WARP", r"warp"),
        ("WARP", r"(?:跃迁|起跳)"),
        ("LOCK", rf"lock(?:\s+up)?\s+{_ARGUMENT}"),
        ("LOCK", rf"锁定\s*{_ARGUMENT}"),
        ("ANCHOR", rf"anchor(?:\s+(?:on|up))?(?:\s+{_ARGUMENT})?"),
        ("ANCHOR", rf"(?:跟走位|锚定|跟随)(?:\s*{_ARGUMENT})?"),
        ("JUMP", r"jump(?:\s+jump)?"),
        ("JUMP", r"(?:过门|跳跃|跳)"),
        ("FREE_BURN", r"free\s+burn"),
        ("FREE_BURN", r"(?:自由烧|自由航行)"),
        ("PROP_ON", r"(?:prop|mwd|ab)\s+on"),
        ("PROP_ON", r"(?:开推|开启推进|推进开启)"),
        ("OVERHEAT", r"overheat(?:\s+(?:guns|prop|everything))?"),
        ("OVERHEAT", r"(?:超载|过热)(?:武器|推进)?"),
        ("BUBBLE_UP", r"(?:bubble|bubbles)\s+up"),
        ("BUBBLE_UP", r"(?:拉泡泡|下泡泡|放泡泡)"),
        ("REGROUP", r"regroup(?:\s+(?:on|at))?(?:\s+.+)?"),
        ("REGROUP", r"(?:集合|重新集结)(?:\s*.+)?"),
        ("STAR_BURST", r"star\s*burst"),
        ("STAR_BURST", r"(?:四散|散开)"),
    )


_COMPILED_RULES = tuple(
    (code, re.compile(rf"^(?:{pattern})$", re.IGNORECASE | re.UNICODE))
    for code, pattern in _rules()
)
_CLAUSE_SEPARATOR = re.compile(r"\s*(?:[;；\n]|\s+[>|→]\s+|[,，]\s+(?=[A-Za-z\u3400-\u9fff]))\s*")
_EDGE_PUNCTUATION = " \t\r.!?。！？,，;；:："


def _candidate_clauses(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = " ".join(normalized.split())
    if not normalized:
        return []
    parts = [part.strip(_EDGE_PUNCTUATION) for part in _CLAUSE_SEPARATOR.split(normalized)]
    return [part for part in parts if part]


def classify_commands(
    text: str,
    *,
    max_commands: int = 3,
    doctrine_pack=None,
) -> tuple[CommandMatch, ...]:
    """Classify up to three complete command clauses from original text.

    Substrings never create a card: ``jumping`` and ``primary school`` do not
    match, while ``do not jump`` resolves only to ``DO_NOT_JUMP``.
    """

    if max_commands <= 0:
        return ()
    matches: list[CommandMatch] = []
    for clause in _candidate_clauses(text):
        matched = False
        for code, pattern in _COMPILED_RULES:
            result = pattern.fullmatch(clause)
            if not result:
                continue
            argument = (result.groupdict().get("argument") or "").strip(_EDGE_PUNCTUATION)
            matches.append(CommandMatch(
                code=code,
                category=COMMAND_CATEGORY[code],
                argument=argument,
                source_clause=clause,
            ))
            matched = True
            break
        if not matched and doctrine_pack is not None:
            lookup = getattr(doctrine_pack, 'command_for_phrase', None)
            code = lookup(clause) if callable(lookup) else None
            if code in COMMAND_CATEGORY:
                matches.append(CommandMatch(
                    code=code,
                    category=COMMAND_CATEGORY[code],
                    source_clause=clause,
                ))
        if len(matches) >= max_commands:
            break
    return tuple(matches)
