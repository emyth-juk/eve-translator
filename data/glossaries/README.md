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
