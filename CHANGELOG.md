# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4] - 2026-11-08

### Added
- Tagged Windows releases now submit the exact packaged executable to VirusTotal and include its SHA-256, completed aggregate findings, and live report link in the GitHub release notes.
- Combat, Bilingual, and Mirror overlay modes with a compact status header; click the header's display-mode chip to cycle them.
- Explicit FC, Scout, and Starred speaker roles plus deterministic source-text command cards, drawn as two-line cards with a category-coloured accent: FC-verified cards are vivid, unverified cards are muted.
- True Windows click-through interaction lock with a system-tray action, a **Lock overlays** entry in the overlay menu, and global `Ctrl+Shift+L` shortcut. Locked overlays hide the scroll bar and always follow the newest line.
- Language-pair glossary selection, exact local correction memory, and safe importable doctrine packs.
- A generated multilingual EVE entity index sourced from NoirSoldats' converted CCP SDE SQLite release.
- ID-linked SDE locale mappings that distinguish official type names from fleet shorthand and player nicknames.
- Provider-native DeepL burst batching and opt-in, source-isolated rolling context.
- Truthful Fleet, Local, and Any Chat readiness states in the system tray: Off, Waiting, Active, and Error.
- Automatic recovery when an enabled chat log becomes available after startup.
- Explicit Disabled, Google, and DeepL provider selection with cloud-use explanations.
- Masked DeepL key entry, reveal control, and a provider connection test using only a built-in phrase.
- Pending and failed translation states that keep the original message visible.
- Manual outbound compose window with source/target selection, asynchronous translation, stale-result protection, and explicit clipboard copy.
- DeepL quota refresh, Google local character estimates, and warnings when DeepL usage reaches 80%.
- Opt-in provider fallback that contacts the second cloud provider only after primary failure and explicit consent.
- Schema-v2 doctrine correction packs and constrained custom command types with safe categories and display labels.
- A third Any Chat overlay for one selected Corp, Alliance, intel, or custom channel, with stable per-character selection and relog recovery.
- Global shortcuts can be changed in Settings and take effect on Save; a shortcut Windows refuses is marked in the tray menu.
- Compose remembers the last outbound language, defaults to the fleet's recent language, accepts language names, and adds Cancel and Copy & close.
- Right-click a chat line for actions on that line: copy the translation or original, improve that translation, and set the speaker's role.
- Reading older lines no longer jumps to the bottom when new lines arrive; a "new messages" pill jumps back to the latest line.
- Empty-state hints explain what Combat and Bilingual modes will show.
- A README screenshot gallery, regenerated from the app's own windows by `scripts/render_screenshots.py`.

### Changed
- Release automation now isolates build, VirusTotal scan, and publication permissions; scan failures stop publication, while malicious or suspicious classifications create a draft release for maintainer review.
- VirusTotal receives only the public release executable through its standard public submission service; API credentials, logs, configuration, source bundles, and the release ZIP are never submitted.
- Original chat lines now reach the HUD before provider work and remain visible after translation.
- Analysis and provider calls run on separate workers so a stalled provider cannot hide later originals.
- SDE conversion is delegated to `noirsoldats/eve-sde-converter`; this repository contains only a validated SQLite-to-runtime-index extractor.
- Bundled YAML now owns only aliases, abbreviations, commands, doctrine vocabulary, and slang; exact vanilla SDE mappings are generated instead.
- Cloud translation is disabled by default and is never enabled without an explicit provider choice.
- Existing configurations with a saved DeepL key continue using DeepL; no-key configurations migrate to Disabled instead of silently using Google.
- Ignored languages now apply to the exact detected base language: ignoring `zh` suppresses Chinese only, while Japanese and Korean remain independent choices.
- Settings can now be opened directly from the system tray.
- First-run provider guidance remains active until the player saves an explicit choice.
- Provider calls and connection tests have responsiveness deadlines, and Settings validates DeepL keys and target-language codes before saving.
- Generated SDE loading now runs on the translation thread, and warm cache hits bypass lexicon/entity preprocessing.
- Correction memory now uses one guarded SQLite connection, scoped/global lookup in one query, and a bounded positive/negative cache.
- Fleet, Local, and Any Chat discovery now share one cached directory snapshot on a dedicated worker thread instead of repeatedly scanning on the GUI thread.
- Built-in session labels, defaults, geometry, log routing, and source-owned metadata now come from one descriptor table.
- Release builds now pin runtime and top-level build/test dependencies, validate tag/version consistency, run Ruff and the full test suite before packaging, and embed Windows version metadata.
- Windows releases now publish one `windows_x64` ZIP that keeps the executable, project and third-party notices, checksums, component manifest, and exact license texts together.
- Packaging now includes only the Qt plugins the Windows Widgets app needs, excluding unused PDF, SVG, network, image-codec, alternate-platform, and GPL-only Qt Virtual Keyboard components.
- Release builds use Python 3.15, and runtime and build dependencies were refreshed, including Qt/PySide 6.12.0 (now `PySide6-Essentials` only, without the unused Addons, WebEngine and Pdf wheels), PyInstaller 6.22.3, DeepL SDK 1.33.0 and certifi 2026.10.23, while CI actions are pinned to reviewed commits. CI tests Python 3.11 through 3.15.
- Google translation now uses a small internal, time-bounded mobile-web adapter instead of the historically compromised `deep-translator` package.
- Local-system detection now scans only newly appended log bytes and keeps a bounded cache instead of rereading entire Local logs.
- A DeepL request now gives up after one retry (about 21 s instead of up to 90 s); repeated failures pause the provider for 30 s (5 min for quota or key errors) and the reason is shown once in the overlay header instead of on every line.
- Queued translations older than 30 s are skipped instead of building a backlog during an outage.
- Combat mode no longer sends lines it hides to the cloud provider.
- The opacity setting now applies to the overlay background only; text stays fully opaque, and secondary text scales with the font size.
- Translation updates redraw a single message instead of the whole overlay, and resizing works on every edge with native Windows borders.
- Redesigned overlay: a rounded translucent glass panel, a painted status header (health light, session name, context, mode chip, lock state), Segoe UI and Bahnschrift typography, a slim scroll bar, dark rounded menus and tooltips, and a subtle resize grip.
- Chat lines use a compact minute time column (full time on hover) with wrapped text hanging under the speaker, speaker role badges and accents, and a language-tagged original line that is omitted when it matches the translation.
- Refined default text colours (soft cyan translations, amber highlights). Untouched original defaults (`#00ffff`, `yellow`) migrate once; custom colours are kept.
- Rebuilding an overlay (mode switches, showing it again, style changes) is about 2.5 times faster, and status updates are faster; new lines cost no more than before.
- Chat-log discovery shares one header cache sized to the folder (about 4x faster on large Chatlogs folders), checks for shutdown inside long scans, and pauses while every session is off.
- Settings now scrolls and fits small screens, saves only the values changed in the dialog, and opens once (a second request raises it).
- The default log poll interval is 0.5 s.

### Fixed
- Vanilla SDE names now outrank shorter player aliases by full span, including unchanged or ambiguous locale names that act as no-op guards.
- The stale `铁壁: Keepstar` glossary row is gone: CCP's type IDs map `铁壁` to Fortizar and `星城` to Keepstar.
- Regional targets such as `de-DE` now load the matching base-language glossary (`zh_de.yml`).
- Messages already in the target language no longer consume translation quota.
- Common English fleet commands such as `align`, `jump`, `do not jump`, `hold cloak`, and `primary` are detected consistently.
- Mixed-language messages are no longer suppressed by embedded EVE terms such as `shield` or `armor`.
- Failed provider calls are no longer rendered as successful translations.
- Versioned config migration preserves an explicit provider choice when older fallback files contain an empty DeepL key.
- Saving a provider change now defers unstarted chat lines until the new choice is active, preventing an old provider from receiving the rest of a queued batch.
- Active overlays return to Waiting when their tracked log goes inactive and recover to Active when it resumes.
- Malformed numeric and text settings are normalized at the config boundary instead of breaking startup or Settings.
- Provider-altered EVE placeholders tolerate safe case/spacing changes while missing or duplicated entities still fail closed to the original.
- Google network calls now have real connect/read timeouts, DeepL's outer deadline matches its bounded SDK retries, and provider status snapshots are atomic.
- Internal translation `TypeError` failures no longer trigger duplicate calls or escape the scheduler.
- All built-in combat phrases share one classifier, preventing short calls such as `regroup`, `star burst`, and `bubble up` from being misdetected and sent unnecessarily.
- Generated English HyperNet offers, including punctuation-prefixed and link-artifact variants, no longer consume translation quota.
- Switching Any Chat sources no longer restores stale channel metadata, and a failed source start can no longer be reported as Active.
- Any Chat activity now tolerates the login boundary, accepts current log writes as positive evidence, and fails closed for characters Local has explicitly found offline.
- EVE log timestamps are consistently interpreted as UTC, while implausibly future-dated files no longer keep Fleet, Local, or Any Chat active after a clock correction.
- A second app launch now exits with a clear already-running message instead of competing for both global hotkeys and showing misleading shortcut warnings.
- Global shortcut conflicts now preserve the Windows error, identify the affected action and chord, and appear in one combined notification while tray actions remain usable.
- Persistent chat-log discovery failures preserve the last-good sources, use bounded retry backoff, and become visible Error states instead of silent Waiting states.
- Config and correction-memory files created by a newer schema are detected and left untouched instead of being overwritten or downgraded.
- Partially written UTF-16 chat records are retained for the next poll instead of being dropped, while active-log polling stays responsive.
- Chat-log discovery can no longer delay shutdown indefinitely; exit now uses a bounded wait before cleanup.
- Fleet auto-selection now uses a stable filename tie-breaker when creation and modification times match.
- Cancelling first-run log-folder selection now exits cleanly before worker threads start, and a regular file is no longer accepted as the chat-log directory.
- Overlay geometry is preserved when a session is disabled, an inactive Any Chat source is selected, or the app exits.
- Shutdown now cancels waits for in-flight provider calls and suppresses late results instead of waiting for the full provider deadline.
- Single-character glossary terms no longer rewrite parts of ordinary Chinese words (for example 收到 -> "WTB 到"), and Latin EVE names written directly next to Chinese text are protected.
- Translations into Chinese, Japanese or Korean are no longer discarded when the provider attaches a protected name directly to the surrounding text.
- Short English chat ("GATE RED", "need dps") and system names are no longer detected as foreign languages and sent for translation.
- An unquoted number in a custom glossary (for example `88: Bye`) no longer crashes startup.
- A failed DeepL batch is no longer resent line by line to the same provider.
- Resized overlays no longer snap back to their previous size, and overlays saved on a disconnected monitor are moved back on screen.
- Saving or cancelling Settings no longer reverts changes made meanwhile by discovery or the tray (selected fleet, character or channel, system name, enabled sessions).
- Pressing Enter in a Settings field saves instead of opening the folder picker; Reset Position works and previews immediately.
- A settings file that cannot be read is preserved as `translator_config.json.corrupt-<time>` instead of being replaced with defaults; UTF-8 files with a byte-order mark are read.
- Quitting during a long discovery pass no longer crashes the app, and the window probe no longer waits on the app's own windows.
- At startup Fleet follows the selected fleet's log, and no longer scans the whole folder on the GUI thread while waiting for a fleet.
- Stopped sessions, their overlays, and closed Settings dialogs are released instead of accumulating in memory.
- Lines that contain only EVE names are resolved locally without a provider request.
- Command cards recognise calls to the whole fleet ("全体对齐", "everyone align"), split chained calls ("然后", "then"), and no longer turn "primary dead" into a target.
- DeepL target codes for regional Chinese and Norwegian (zh-TW, zh-CN, nb-NO) are mapped to the variants DeepL accepts.
- The tray tooltip fits the Windows 127-character limit, and a persistent discovery error is announced once.
- A second copy no longer starts when the first runs as administrator.
- A chat line written while a session starts is no longer lost.
- Context menus are released after use instead of accumulating on the overlay.

### Known limitations
- VirusTotal findings are a point-in-time aggregation of independent engines, not a safety guarantee; the linked live report can change after publication.
- Any Chat follows one selected logged source and cannot detect which EVE chat tab is currently visible.
- Windows may reject a global shortcut already owned by another application; the equivalent system-tray action remains available.
- Windows release executables are currently unsigned, so Microsoft Defender SmartScreen may show an unrecognized-app warning.
- Provider-native burst batching is available for DeepL only; Google requests remain sequential.
- The bundled SDE entity index is a pinned snapshot; entities introduced by later EVE builds require a regenerated index.
- Keyless Google mode depends on a public mobile web endpoint, not an official API, and may be rate-limited or changed by Google.

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
    - Automated Releases (`release.yml`) triggered by tags, building Windows executables and publishing release assets.
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
