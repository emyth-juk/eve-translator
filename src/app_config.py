import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

CONFIG_DIR_NAME = ".eve_translator"
CONFIG_FILE_NAME = "translator_config.json"
LEGACY_OVERLAY_CONFIG_FILE_NAME = "overlay_config.json"

SHARED_CONFIG_KEYS = frozenset({
    'opacity', 'font_size', 'auto_scroll',
    'ignored_languages', 'target_language', 'deepl_api_key',
    'color_default', 'color_translated', 'color_highlight', 'log_dir',
    'fleet_inactive_threshold', 'fleet_auto_switch', 'fleet_scan_interval',
    'fleet_history_lines', 'polling_interval'
})


def get_default_config(legacy_overlay: Mapping[str, Any] | None = None) -> dict:
    legacy_overlay = legacy_overlay or {}
    return {
        'shared': {
            'opacity': legacy_overlay.get('opacity', 0.8),
            'font_size': legacy_overlay.get('font_size', 10),
            'auto_scroll': legacy_overlay.get('auto_scroll', True),
            'ignored_languages': legacy_overlay.get('ignored_languages', ['en']),
            'target_language': legacy_overlay.get('target_language', 'en'),
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
        },
    }


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

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def default_path() -> Path:
        return Path.home() / CONFIG_DIR_NAME / CONFIG_FILE_NAME

    def load(self) -> dict:
        legacy_overlay = self._read_json(self.legacy_dir / LEGACY_OVERLAY_CONFIG_FILE_NAME)
        config = get_default_config(legacy_overlay)

        for candidate in self._load_candidates():
            loaded = self._read_json(candidate)
            if loaded:
                self._merge_config(config, loaded)

        return config

    def save(self, config: dict) -> None:
        to_save = copy.deepcopy(config)
        remove_shared_keys_from_sessions(to_save)

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, 'w', encoding='utf-8') as config_file:
                json.dump(to_save, config_file, indent=2)
        except Exception as exc:
            logger.error("Error saving config to %s: %s", self.path, exc)

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
