# EVE Translator Glossaries

This directory contains glossary files for EVE Online terminology translation.

## File Format

Each glossary file maps source language terms to target language equivalents:
- Filename pattern: `{source}_{target}.yml`
- Example: `zh_en.yml` (Chinese to English)

## Structure

Glossaries are organized by category:
- `ships`: Ship types and names
- `modules`: Ship modules and equipment
- `locations`: Systems, regions, and space types
- `actions`: Player actions and activities
- `commands`: Fleet commands and instructions
- `slang`: Common phrases and abbreviations

## Adding Custom Terms

Create your own glossary in `~/.eve_translator/glossaries/`:

Example: `~/.eve_translator/glossaries/custom_zh_en.yml`
```yaml
meta:
  source_lang: zh
  target_lang: en
  description: "My custom terms"

ships:
  custom:
    我的船: My Ship

commands:
  alliance:
    特殊指令: Special Command
```

Custom terms override bundled terms.

Regional pairs layer over their base language. For example, `de-DE` first
loads `zh_de.yml`, then an optional `custom_zh_de-de.yml`; the regional custom
file overrides matching base terms without losing the rest of the base glossary.

## SDE names versus glossary terms

Official published type names and their CCP-localized translations belong in
`data/generated/protected_entities.json`, generated from NoirSoldats' converted
SDE database. Do not duplicate a vanilla source/target name pair in a bundled
YAML glossary.

Glossaries remain the owner of player language that the SDE cannot express:

- shortened hull stems such as `末日沙场` for the official `末日沙场级`;
- nicknames such as `Geddon`;
- abbreviations such as `YST`, `DMS`, `HIC`, and `DIC`;
- fleet commands, doctrine terms, and community slang.

Cleanup is identity-based, not value-based: remove a bundled term when its exact
source is already an official localized SDE name. The ID-linked target then
becomes authoritative; this also corrects stale cross-ID rows such as the old
`铁壁: Keepstar` mapping (`铁壁` is Fortizar, while `星城` is Keepstar). A value
such as `Armageddon` does not make `Geddon` or `末日沙场` redundant.

## Contributing

To add new terms:
1. Find the appropriate category
2. Add the term with proper indentation
3. Include comments for context (optional)
4. Submit a pull request

## Ignored Phrases

The file `ignored_phrases.yml` contains terms that should **never** be translated.

### Structure
- `force_ignore`: List of keywords that force the translator to skip the line (e.g., market items, contract types).
- `slang`: List of regex patterns to ignore common internet slang (e.g., `lol`, `gf`, `o7`).

### Example
```yaml
force_ignore:
  - 'DoNotTranslateMe'
  - 'SpecificItemName'

slang:
  - '^kekw$'
```

## Language Codes

The translator uses **ISO 639-1** two-letter language codes for `source_lang` and `target_lang`.

- Common Codes:
  - English: `en`
  - Chinese (Simplified): `zh`
  - German: `de`
  - French: `fr`
  - Russian: `ru`
  - Japanese: `ja`
  - Korean: `ko`

For a full list of supported codes, please refer to the [Wikipedia ISO 639-1 Code List](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes).
