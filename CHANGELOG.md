# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4] - 2026-07-11

### Added
- Combat, Bilingual, and Mirror overlay modes with a compact status header.
- Explicit FC, Scout, and Starred speaker roles plus deterministic source-text command cards.
- True Windows click-through interaction lock with a system-tray action and global `Ctrl+Shift+L` shortcut.
- Language-pair glossary selection, exact local correction memory, and safe importable doctrine packs.
- A generated multilingual EVE entity index sourced from NoirSoldats' converted CCP SDE SQLite release.
- ID-linked SDE locale mappings that distinguish official type names from fleet shorthand and player nicknames.
- Provider-native DeepL burst batching and opt-in, source-isolated rolling context.
- Truthful Fleet and Local readiness states in the system tray: Off, Waiting, Active, and Error.
- Automatic recovery when an enabled chat log becomes available after startup.
- Explicit Disabled, Google, and DeepL provider selection with cloud-use explanations.
- Masked DeepL key entry, reveal control, and a provider connection test using only a built-in phrase.
- Pending and failed translation states that keep the original message visible.

### Changed
- Original chat lines now reach the HUD before provider work and remain visible after translation.
- Analysis and provider calls run on separate workers so a stalled provider cannot hide later originals.
- SDE conversion is delegated to `noirsoldats/eve-sde-converter`; this repository contains only a validated SQLite-to-runtime-index extractor.
- Bundled YAML now owns only aliases, abbreviations, commands, doctrine vocabulary, and slang; exact vanilla SDE mappings are generated instead.
- Cloud translation is disabled by default and is never enabled without an explicit provider choice.
- Existing configurations with a saved DeepL key continue using DeepL; no-key configurations migrate to Disabled instead of silently using Google.
- Settings can now be opened directly from the system tray.
- First-run provider guidance remains active until the player saves an explicit choice.
- Provider calls and connection tests have responsiveness deadlines, and Settings validates DeepL keys and target-language codes before saving.

### Fixed
- Vanilla SDE names now outrank shorter player aliases by full span, including unchanged or ambiguous locale names that act as no-op guards.
- The stale `铁壁: Keepstar` glossary row is gone: CCP's type IDs map `铁壁` to Fortizar and `星城` to Keepstar.
- Regional targets such as `de-DE` now load the matching base-language glossary (`zh_de.yml`).
- Messages already in the target language no longer consume translation quota.
- Common English fleet commands such as `align`, `jump`, `do not jump`, `hold cloak`, and `primary` are detected consistently.
- Mixed-language messages are no longer suppressed by embedded EVE terms such as `shield` or `armor`.
- Failed provider calls are no longer rendered as successful translations.
- Saving a provider change now defers unstarted chat lines until the new choice is active, preventing an old provider from receiving the rest of a queued batch.
- Active overlays return to Waiting when their tracked log goes inactive and recover to Active when it resumes.
- Malformed numeric and text settings are normalized at the config boundary instead of breaking startup or Settings.

## [1.3] - 2026-06-29

### Changed
- Faster, lighter overlays with batched rendering and capped visible history.
- Reduced duplicate translation/API calls with translation and language-decision caching.
- Lower idle overhead with file-system based log watching and optimized log backfill.
- More reliable settings/config handling, tests, and executable build process.

## [0.2.3] - 2025-12-19

### Changed
- **Overlay Resizing**: Improved resizing with native OS support (all edges/corners), better cursor feedback, and a subtle visual border.
- **Ignored Phrases**: Added `omega` and `Triglavian` to default ignore list.

### Fixed
- **DeepL API**: Fixed `400 Bad Request` errors and reduced unnecessary translations for short text.
- **Overlay Grip**: Fixed bottom-right grip being obscured by text.

## [0.2.2] - 2025-12-17

### Added
- **Overlay Enhancements**: 
    - Added a visual resize grip to the bottom-right corner.
    - Lowered minimum window size to 120x80 for more compact layouts.

### Fixed
- **Settings**: Fixed a crash preventing the DeepL API key from being saved via the GUI (`AttributeError`).

### Documentation
- **README**: Updated "Controls & Interactions" section to clarify moving/resizing and context menu options.

## [0.2.1] - 2025-12-17

### Fixed
- **Overlay Resizing**: Fixed a crash caused by incorrect cursor handling (`QCursor` vs `QTextCursor`).
- **Release Workflow**: Removed redundant source zip creation from GitHub Actions.

### Documentation
- **README**: 
    - Added instructions for using the `exe` vs source.
    - Added Settings GUI configuration guide.
    - Added "How to get a Free DeepL API Key".
    - Added screenshots.

## [0.2.0] - 2025-12-17

### Added
- **CI/CD Pipeline**: 
    - GitHub Actions workflow (`run_tests.yml`) for cross-platform testing (Windows/Ubuntu).
    - Automated Releases (`release.yml`) triggered by tags, building signed executables and source zips.
    - Automated release note extraction.
- **Testing**: Added comprehensive test suites:
    - `test_integration_pipeline.py`: End-to-End flow verification.
    - `test_gui_formatting.py`: HTML message rendering checks.
    - `test_translator_errors.py`: API error robustness.
    - `test_config_persistence.py`: Configuration saving verification.

### Changed
- **Glossary Logic**: Improved term replacement to use regex word boundaries (`\b`) for alphanumeric terms (fixing collisions like "00" matching inside "1600") while retaining literal matching for CJK terms.
- **Logging**: Moved logging setup to `__main__` guard to prevent side-effects during testing.
- **Dependencies**: Removed deprecated `datetime.utcnow()` calls in favor of timezone-aware `datetime.now(datetime.timezone.utc)`.

### Fixed
- Fixed `DeprecationWarning` in Overlay.
- Fixed `PermissionError` in integration tests on Windows by ensuring proper file handle cleanup.
- Fixed logic in `test_ignored_phrases` to correctly handle default ignored languages.

## [0.1.0] - 2025-12-16

### Added
- **Dual Overlays**: Separate windows for Fleet chat and Local chat.
- **Glossary System**: 
    - Translation pre-processing for EVE slang (e.g., "毒蜥" -> "Gila").
    - Support for `zh_en` and `zh_de` language pairs.
    - Custom user glossaries supported in `%USERPROFILE%/.eve_translator/glossaries/`.
    - `ignored_phrases.yml` configuration for blocking specific terms/slang.
- **Translation**: Integration with DeepL API (Free tier).
- **Automation**: 
    - Auto-detection of active EVE character logs.
    - Local system tracking.
- **UI**: 
    - System tray icon.
    - Settings menu for opacity, font size, and paths.
    - Adjustable overlay windows with click-through support in EVE.

### Changed
- Refactored `LanguageDetector` to be configurable.
- Updated build process to bundle all data files.

### Security
- API keys are stored in `translator_config.json` (not shared).
