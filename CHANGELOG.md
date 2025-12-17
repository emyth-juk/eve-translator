# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


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
