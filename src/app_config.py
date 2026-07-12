import copy
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Mapping

from src.core.languages import normalize_locale

logger = logging.getLogger(__name__)

CONFIG_SCHEMA_VERSION = 1
CONFIG_DIR_NAME = ".eve_translator"
CONFIG_FILE_NAME = "translator_config.json"
LEGACY_OVERLAY_CONFIG_FILE_NAME = "overlay_config.json"
TRANSLATION_PROVIDER_KEY = "translation_provider"
TRANSLATION_PROVIDER_DISABLED = "disabled"
TRANSLATION_PROVIDER_GOOGLE = "google"
TRANSLATION_PROVIDER_DEEPL = "deepl"
TRANSLATION_PROVIDER_MODES = frozenset({
    TRANSLATION_PROVIDER_DISABLED,
    TRANSLATION_PROVIDER_GOOGLE,
    TRANSLATION_PROVIDER_DEEPL,
})
DISPLAY_MODE_MIRROR = "mirror"
DISPLAY_MODE_BILINGUAL = "bilingual"
DISPLAY_MODE_COMBAT = "combat"
DISPLAY_MODES = frozenset({
    DISPLAY_MODE_MIRROR,
    DISPLAY_MODE_BILINGUAL,
    DISPLAY_MODE_COMBAT,
})

SHARED_CONFIG_KEYS = frozenset({
    'opacity', 'font_size', 'auto_scroll',
    'ignored_languages', 'target_language', 'translation_provider',
    'provider_choice_confirmed', 'deepl_api_key',
    'color_default', 'color_translated', 'color_highlight', 'log_dir',
    'fleet_inactive_threshold', 'fleet_auto_switch', 'fleet_scan_interval',
    'fleet_history_lines', 'polling_interval',
    'speaker_roles', 'global_hotkey',
    'entity_protection', 'entity_index_path', 'burst_optimization',
    'burst_batch_size', 'context_assist', 'context_messages',
    'active_doctrine_pack',
})


def resolve_translation_provider(shared_config: Mapping[str, Any]) -> str:
    """Return a safe provider mode, including migration for pre-v1.4 configs.

    Before v1.4, provider selection was implicit: a non-empty DeepL key selected
    DeepL and an empty key selected Google. Existing DeepL users keep that
    behavior. Configs without a key now migrate to ``disabled`` so a cloud
    provider is never selected without an explicit choice.
    """
    configured = shared_config.get(TRANSLATION_PROVIDER_KEY)
    if configured is not None:
        if not isinstance(configured, str):
            return TRANSLATION_PROVIDER_DISABLED
        normalized = configured.strip().lower()
        if normalized in TRANSLATION_PROVIDER_MODES:
            return normalized
        return TRANSLATION_PROVIDER_DISABLED

    deepl_api_key = shared_config.get('deepl_api_key', '')
    if isinstance(deepl_api_key, str) and deepl_api_key.strip():
        return TRANSLATION_PROVIDER_DEEPL
    return TRANSLATION_PROVIDER_DISABLED


def normalize_translation_provider_config(shared_config: dict) -> str:
    """Normalize and persist the provider mode in a shared config mapping."""
    provider = resolve_translation_provider(shared_config)
    shared_config[TRANSLATION_PROVIDER_KEY] = provider
    return provider


def _bounded_number(value, default, minimum, maximum, *, integer=False):
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    number = min(maximum, max(minimum, number))
    return int(number) if integer else number


def _config_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {'true', 'yes', '1'}:
            return True
        if normalized in {'false', 'no', '0'}:
            return False
    return default


def _config_text(value, default=''):
    return value if isinstance(value, str) else default


def _document_schema_version(config: Mapping[str, Any]) -> int:
    version = config.get('schema_version', 0)
    if type(version) is not int or version < 0:
        return 0
    return version


def _migrate_provider_fields(
    shared: dict,
    *,
    legacy_schema: bool,
) -> tuple[str | None, bool | None]:
    """Materialize legacy provider state once and return its provenance.

    An empty legacy DeepL key represented implicit Google in pre-1.4 builds.
    It deliberately does *not* materialize a provider field: doing so would let
    an incidental empty key in a later candidate erase an earlier explicit
    choice. A standalone no-key legacy config still inherits the safe Disabled
    default and remains unconfirmed.
    """

    if TRANSLATION_PROVIDER_KEY in shared:
        configured = shared.get(TRANSLATION_PROVIDER_KEY)
        valid = (
            isinstance(configured, str)
            and configured.strip().lower() in TRANSLATION_PROVIDER_MODES
        )
        if valid:
            shared[TRANSLATION_PROVIDER_KEY] = configured.strip().lower()
            confirmation = (
                _config_bool(shared.get('provider_choice_confirmed'), False)
                if 'provider_choice_confirmed' in shared
                else True
            )
            return 'explicit', confirmation
        shared[TRANSLATION_PROVIDER_KEY] = TRANSLATION_PROVIDER_DISABLED
        shared['provider_choice_confirmed'] = False
        return 'invalid', False

    if not legacy_schema or 'deepl_api_key' not in shared:
        return None, None

    api_key = shared.get('deepl_api_key')
    if isinstance(api_key, str) and api_key.strip():
        shared[TRANSLATION_PROVIDER_KEY] = TRANSLATION_PROVIDER_DEEPL
        confirmation = (
            _config_bool(shared.get('provider_choice_confirmed'), False)
            if 'provider_choice_confirmed' in shared
            else True
        )
        return 'legacy_deepl', confirmation

    # Pre-1.4 selected Google implicitly for this shape. It now fails closed,
    # but must not override a provider explicitly chosen in another source.
    return 'legacy_google', False


def _migrate_config_document(
    loaded: Mapping[str, Any],
) -> tuple[dict, str | None, bool | None, bool]:
    """Upgrade one structured config document to the current schema in memory."""

    version = _document_schema_version(loaded)
    if version > CONFIG_SCHEMA_VERSION:
        logger.error(
            "Ignoring config with unsupported schema version %s (maximum %s)",
            version,
            CONFIG_SCHEMA_VERSION,
        )
        return {}, None, None, False

    migrated = copy.deepcopy(dict(loaded))
    shared = migrated.get('shared')
    if not isinstance(shared, dict):
        shared = {}
        migrated['shared'] = shared
    origin, confirmation = _migrate_provider_fields(
        shared,
        legacy_schema=version < CONFIG_SCHEMA_VERSION,
    )
    changed = version < CONFIG_SCHEMA_VERSION
    migrated['schema_version'] = CONFIG_SCHEMA_VERSION
    return migrated, origin, confirmation, changed


def normalize_config(config: dict) -> dict:
    """Normalize user-editable values to safe runtime types and UI ranges."""
    config['schema_version'] = CONFIG_SCHEMA_VERSION
    shared = config.setdefault('shared', {})
    if not isinstance(shared, dict):
        shared = {}
        config['shared'] = shared

    shared['opacity'] = _bounded_number(shared.get('opacity'), 0.8, 0.2, 1.0)
    shared['font_size'] = _bounded_number(
        shared.get('font_size'), 10, 8, 36, integer=True
    )
    shared['auto_scroll'] = _config_bool(shared.get('auto_scroll'), True)

    ignored = shared.get('ignored_languages', ['en'])
    if isinstance(ignored, str):
        ignored = ignored.split(',')
    if not isinstance(ignored, (list, tuple, set)):
        ignored = ['en']
    shared['ignored_languages'] = [
        normalize_locale(language)
        for language in ignored
        if isinstance(language, str) and language.strip()
    ]

    shared['target_language'] = normalize_locale(
        shared.get('target_language'), 'en'
    )
    normalize_translation_provider_config(shared)
    shared['provider_choice_confirmed'] = _config_bool(
        shared.get('provider_choice_confirmed'), False
    )
    shared['deepl_api_key'] = _config_text(shared.get('deepl_api_key')).strip()

    shared['color_default'] = _config_text(
        shared.get('color_default'), '#e0e0e0'
    )
    shared['color_translated'] = _config_text(
        shared.get('color_translated'), '#00ffff'
    )
    shared['color_highlight'] = _config_text(
        shared.get('color_highlight'), 'yellow'
    )
    shared['log_dir'] = _config_text(
        shared.get('log_dir'),
        os.path.expanduser("~/Documents/EVE/logs/Chatlogs"),
    )
    shared['fleet_inactive_threshold'] = _bounded_number(
        shared.get('fleet_inactive_threshold'), 1800, 60, 7200, integer=True
    )
    shared['fleet_auto_switch'] = _config_bool(
        shared.get('fleet_auto_switch'), True
    )
    shared['fleet_scan_interval'] = _bounded_number(
        shared.get('fleet_scan_interval'), 10, 5, 300, integer=True
    )
    shared['fleet_history_lines'] = _bounded_number(
        shared.get('fleet_history_lines'), 5, 0, 50, integer=True
    )
    shared['polling_interval'] = _bounded_number(
        shared.get('polling_interval'), 1.0, 0.1, 10.0
    )
    speaker_roles = shared.get('speaker_roles', {})
    if not isinstance(speaker_roles, dict):
        speaker_roles = {}
    from src.core.combat import normalize_speaker_roles
    shared['speaker_roles'] = normalize_speaker_roles(speaker_roles)
    # Interaction lock is runtime-only: never relaunch click-through because a
    # stale config survived a crash or a hotkey collision.
    shared.pop('interaction_locked', None)
    hotkey = _config_text(shared.get('global_hotkey'), 'Ctrl+Shift+L').strip()
    shared['global_hotkey'] = hotkey or 'Ctrl+Shift+L'
    shared['entity_protection'] = _config_bool(
        shared.get('entity_protection'), True
    )
    shared['entity_index_path'] = _config_text(
        shared.get('entity_index_path'), ''
    ).strip()
    shared['burst_optimization'] = _config_bool(
        shared.get('burst_optimization'), True
    )
    shared['burst_batch_size'] = _bounded_number(
        shared.get('burst_batch_size'), 5, 1, 10, integer=True
    )
    shared['context_assist'] = _config_bool(
        shared.get('context_assist'), False
    )
    shared['context_messages'] = _bounded_number(
        shared.get('context_messages'), 3, 1, 10, integer=True
    )
    shared['active_doctrine_pack'] = _config_text(
        shared.get('active_doctrine_pack'), ''
    ).strip()

    sessions = config.setdefault('sessions', {})
    if not isinstance(sessions, dict):
        sessions = {}
        config['sessions'] = sessions
    session_defaults = {
        'fleet': {
            'enabled': True,
            'x': 100,
            'y': 100,
            'w': 300,
            'h': 200,
            'background_color': '#33001a',
            'title_prefix': '[FLEET]',
        },
        'local': {
            'enabled': False,
            'x': 410,
            'y': 100,
            'w': 300,
            'h': 200,
            'background_color': '#001a33',
            'title_prefix': '[LOCAL]',
        },
    }
    for session_id, defaults in session_defaults.items():
        session = sessions.setdefault(session_id, {})
        if not isinstance(session, dict):
            session = {}
            sessions[session_id] = session
        session['enabled'] = _config_bool(
            session.get('enabled'), defaults['enabled']
        )
        for coordinate in ('x', 'y'):
            session[coordinate] = _bounded_number(
                session.get(coordinate),
                defaults[coordinate],
                -100_000,
                100_000,
                integer=True,
            )
        for dimension in ('w', 'h'):
            session[dimension] = _bounded_number(
                session.get(dimension),
                defaults[dimension],
                120,
                10_000,
                integer=True,
            )
        session['background_color'] = _config_text(
            session.get('background_color'), defaults['background_color']
        )
        session['title_prefix'] = _config_text(
            session.get('title_prefix'), defaults['title_prefix']
        )
        display_mode = _config_text(
            session.get('display_mode'), DISPLAY_MODE_MIRROR
        ).strip().lower()
        session['display_mode'] = (
            display_mode if display_mode in DISPLAY_MODES else DISPLAY_MODE_MIRROR
        )
        session['compact_header'] = _config_bool(
            session.get('compact_header'), True
        )
        session['command_cards'] = _config_bool(
            session.get('command_cards'), True
        )

    return config


def get_default_config(legacy_overlay: Mapping[str, Any] | None = None) -> dict:
    legacy_overlay = legacy_overlay or {}
    legacy_provider = resolve_translation_provider(legacy_overlay)
    config = {
        'schema_version': CONFIG_SCHEMA_VERSION,
        'shared': {
            'opacity': legacy_overlay.get('opacity', 0.8),
            'font_size': legacy_overlay.get('font_size', 10),
            'auto_scroll': legacy_overlay.get('auto_scroll', True),
            'ignored_languages': legacy_overlay.get('ignored_languages', ['en']),
            'target_language': legacy_overlay.get('target_language', 'en'),
            'translation_provider': legacy_provider,
            'provider_choice_confirmed': False,
            'deepl_api_key': legacy_overlay.get('deepl_api_key', ''),
            'color_default': legacy_overlay.get('color_default', '#e0e0e0'),
            'color_translated': legacy_overlay.get('color_translated', '#00ffff'),
            'color_highlight': legacy_overlay.get('color_highlight', 'yellow'),
            'log_dir': os.path.expanduser("~/Documents/EVE/logs/Chatlogs"),
            'fleet_inactive_threshold': 1800,
            'fleet_auto_switch': True,
            'fleet_scan_interval': 10,
            'fleet_history_lines': 5,
            'polling_interval': 1.0,
            'speaker_roles': {},
            'global_hotkey': 'Ctrl+Shift+L',
            'entity_protection': True,
            'entity_index_path': '',
            'burst_optimization': True,
            'burst_batch_size': 5,
            'context_assist': False,
            'context_messages': 3,
            'active_doctrine_pack': '',
        },
        'sessions': {
            'fleet': {
                'enabled': True,
                'x': legacy_overlay.get('x', 100),
                'y': legacy_overlay.get('y', 100),
                'w': legacy_overlay.get('w', 300),
                'h': legacy_overlay.get('h', 200),
                'background_color': '#33001a',
                'title_prefix': '[FLEET]',
                'display_mode': DISPLAY_MODE_MIRROR,
                'compact_header': True,
                'command_cards': True,
            },
            'local': {
                'enabled': False,
                'x': 410,
                'y': 100,
                'w': 300,
                'h': 200,
                'background_color': '#001a33',
                'title_prefix': '[LOCAL]',
                'display_mode': DISPLAY_MODE_MIRROR,
                'compact_header': True,
                'command_cards': True,
            },
        },
    }
    return normalize_config(config)


def filter_session_config(config: Mapping[str, Any]) -> dict:
    return {key: value for key, value in config.items() if key not in SHARED_CONFIG_KEYS}


def remove_shared_keys_from_sessions(config: dict) -> dict:
    sessions = config.get('sessions', {})
    if not isinstance(sessions, dict):
        return config

    for session_config in sessions.values():
        if not isinstance(session_config, dict):
            continue
        for key in SHARED_CONFIG_KEYS:
            session_config.pop(key, None)
    return config


def sanitize_config_for_log(config: dict) -> dict:
    sanitized = copy.deepcopy(config)

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                if key == 'deepl_api_key':
                    redacted[key] = "<redacted>" if item else ""
                else:
                    redacted[key] = redact(item)
            return redacted
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return redact(sanitized)


class ConfigStore:
    def __init__(self, path: str | Path | None = None, legacy_dir: str | Path | None = None):
        self._path = Path(path) if path is not None else self.default_path()
        self.legacy_dir = Path(legacy_dir) if legacy_dir is not None else Path.cwd()
        self.provider_selection_required = False
        self.migrated_from_implicit_google = False

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def default_path() -> Path:
        return Path.home() / CONFIG_DIR_NAME / CONFIG_FILE_NAME

    def load(self) -> dict:
        legacy_overlay = self._read_json(
            self.legacy_dir / LEGACY_OVERLAY_CONFIG_FILE_NAME
        )
        migrated_overlay = copy.deepcopy(legacy_overlay)
        overlay_origin, overlay_confirmation = _migrate_provider_fields(
            migrated_overlay,
            legacy_schema=True,
        )
        config = get_default_config(migrated_overlay)
        provider_origin = overlay_origin or 'default'
        provider_choice_confirmed = overlay_confirmation
        migrated_any = bool(legacy_overlay)
        primary_is_current = False

        for candidate in self._load_candidates():
            loaded = self._read_json(candidate)
            if not loaded:
                continue
            if (
                candidate.resolve(strict=False) == self.path.resolve(strict=False)
                and _document_schema_version(loaded) == CONFIG_SCHEMA_VERSION
            ):
                primary_is_current = True

            migrated, origin, confirmation, changed = _migrate_config_document(loaded)
            if not migrated:
                continue
            # Candidate order is precedence order, except an empty legacy key
            # cannot erase an explicit choice from an earlier source.
            accept_state = origin is not None and not (
                origin == 'legacy_google'
                and provider_origin in {'explicit', 'legacy_deepl', 'invalid'}
            )
            if origin == 'legacy_google' and not accept_state:
                migrated_shared = migrated.get('shared', {})
                migrated_shared.pop('deepl_api_key', None)
                migrated_shared.pop('provider_choice_confirmed', None)

            self._merge_config(config, migrated)
            migrated_any = migrated_any or changed
            if accept_state:
                provider_origin = origin
                provider_choice_confirmed = confirmation

        normalize_config(config)
        if provider_origin == 'invalid':
            provider_choice_confirmed = False
        elif provider_choice_confirmed is None:
            provider_choice_confirmed = provider_origin in {'explicit', 'legacy_deepl'}
        config['shared']['provider_choice_confirmed'] = provider_choice_confirmed

        self.migrated_from_implicit_google = provider_origin == 'legacy_google'
        self.provider_selection_required = not provider_choice_confirmed

        # Persist the consolidated schema once. If a current primary already
        # exists, legacy source files are only lower-precedence fallbacks and do
        # not trigger a redundant rewrite on every launch.
        if migrated_any and not primary_is_current and not self.save(config):
            logger.warning("Could not persist migrated config schema to %s", self.path)

        return config

    def save(self, config: dict) -> bool:
        to_save = copy.deepcopy(config)
        remove_shared_keys_from_sessions(to_save)
        normalize_config(to_save)

        temp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, 'w', encoding='utf-8') as config_file:
                json.dump(to_save, config_file, indent=2)
                config_file.flush()
                os.fsync(config_file.fileno())
            os.replace(temp_path, self.path)
            return True
        except Exception as exc:
            logger.error("Error saving config to %s: %s", self.path, exc)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _load_candidates(self) -> list[Path]:
        candidates = [
            self.legacy_dir / CONFIG_FILE_NAME,
            self.path,
        ]
        deduped = []
        seen = set()
        for candidate in candidates:
            key = str(candidate.resolve(strict=False)).casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.exists():
            return {}

        try:
            with open(path, 'r', encoding='utf-8') as config_file:
                loaded = json.load(config_file)
                return loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.error("Error loading config from %s: %s", path, exc)
            return {}

    @staticmethod
    def _merge_config(config: dict, loaded: dict) -> None:
        shared = loaded.get('shared')
        if isinstance(shared, dict):
            config['shared'].update(shared)

        sessions = loaded.get('sessions')
        if isinstance(sessions, dict):
            for session_id, session_config in sessions.items():
                if not isinstance(session_config, dict):
                    continue
                if session_id not in config['sessions']:
                    config['sessions'][session_id] = {}
                config['sessions'][session_id].update(session_config)
