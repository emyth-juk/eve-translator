# Protected entity index

This repository does not convert CCP's YAML or JSONL SDE. That compatibility
work is delegated to the MIT-licensed
[NoirSoldats EVE SDE Converter](https://github.com/noirsoldats/eve-sde-converter).
The local `build_protected_entities.py` script is only a read-only SQLite
extractor for the small name index consumed by the translator.

## Release workflow

1. Open the converter's
   [latest release](https://github.com/noirsoldats/eve-sde-converter/releases/latest).
2. Download `eve.db` for localized type names and constellation names. The
   smaller `eve-stripped.db` is also supported, but its release schema omits
   `trnTranslations` and `mapConstellations`; the generated capability metadata
   records that limitation.
3. Copy the asset's `digest` (`sha256:...`) and release tag from the GitHub
   release API or release page. Never commit either SQLite asset to this repo.
4. Run the extractor from the repository root:

   ```powershell
   python scripts/build_protected_entities.py `
     --database C:\tmp\eve.db `
     --output data\generated\protected_entities.json `
     --release sde-3430261 `
     --commit <converter-commit-sha> `
     --sha sha256:<release-asset-digest> `
     --profile combat `
     --locales en de es fr ja ko ru zh
   ```

5. Run `python -m pytest -q tests/test_build_protected_entities.py` and review
   the source/capability metadata in the generated JSON before packaging it.

The `combat` profile contains published types from inventory category IDs 6,
7, 8, 18, 22, 23, 65, 66, and 87, plus regions, solar systems, and (when
available) constellations. The `all` profile includes every published inventory
category. Type publication is enforced through `invTypes`, `invGroups`, and
`invCategories` together, so an unpublished parent cannot leak into the index.

Localized type names come only from `trnTranslations` rows with `tcID=8`.
Locale aliases such as `en-US`, `de-DE`, `ja-JP`, and `zh-CN` resolve to the
language IDs stored by the converter. Names are normalized to NFC, whitespace
is collapsed, and the output is deterministic. Schema 2 also emits a
`localized_entities` record for every selected published type, keyed by its
stable `type_id`; canonical English comes from `invTypes`, while requested
`tcID=8` names augment the record when available. Stripped databases therefore
produce valid English-only records. ASCII/Latin names retain exact
case to avoid false positives such as the ship `Raven` versus the common noun
`raven`; non-Latin names can match case-insensitively.

The extractor opens SQLite in read-only/query-only mode, disables extension
loading, validates `PRAGMA quick_check`, validates every required table and
column, and optionally verifies the release asset's SHA-256 before reading it.
It never downloads data and contains no CCP SDE parser.
