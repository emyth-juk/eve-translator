# Doctrine pack format

A doctrine pack is a flat ZIP containing four UTF-8, safe-YAML base files and,
for schema v2, an optional `corrections.yml`. Import it from **Settings →
Translation Intelligence → Import ZIP**. One pack can be active at a time.
Packs are data-only: they cannot nominate trusted FC or scout characters, run
code, use regular expressions, or target another installed doctrine's scope.

Schema v1 packs remain supported. Use schema v2 for exact corrections or custom
command types.

## Manifest

`manifest.yml`:

```yaml
schema_version: 2
id: armor-hacs
name: Armor HACs
version: 1.0.0
description: Alliance terminology for the armor wing.
```

## Terms and protected entities

`terms.yml` contains short vocabulary, aliases, and abbreviations. Language
pairs are exact; a regional source such as `zh-Hans` does not silently select
`zh`.

```yaml
pairs:
  zh_en:
    守门: Hold the gate
  de->en:
    Ziel: Target
```

`protected_entities.yml` contains names that cloud translation must preserve:

```yaml
entities:
  - X47L-Q
  - Baltec Megathron
```

## Commands

`commands.yml` maps literal complete phrases to built-in command codes. Values
are never treated as regular expressions:

```yaml
commands:
  ALIGN:
    - form on the outgate
  CEASE_FIRE:
    - red cycle
```

Schema v2 can attach a source-language hint to a built-in command phrase set.
This is useful for short, doctrine-only calls that statistical language
detection cannot identify reliably. The list form above remains valid in both
schema versions.

```yaml
commands:
  CEASE_FIRE:
    source_language: en
    phrases:
      - red cycle
```

Supported built-in codes are `HOLD`, `DO_NOT_JUMP`, `JUMP`, `ALIGN`, `WARP`,
`ANCHOR`, `FREE_BURN`, `PRIMARY`, `SWITCH_PRIMARY`, `LOCK`, `CEASE_FIRE`,
`PROP_ON`, `PROP_OFF`, `OVERHEAT`, `BUBBLE_UP`, `BUBBLE_DOWN`, `REGROUP`, and
`STAR_BURST`.

Schema v2 may define new data-only command types. Codes must be 2–40 uppercase
letters, digits, or underscores and cannot replace a built-in code. Categories
are limited to `HOLD`, `MOVE`, `TARGET`, `TACKLE`, `SUPPORT`, and `INTEL`:

```yaml
commands: {}
custom_commands:
  BASTION_UP:
    category: SUPPORT
    label: Bastion up
    source_language: en
    phrases:
      - green bastion
```

`source_language` is optional, accepts a normalized language or locale code,
and influences translation routing only when command matches cover the entire
message. The label is display text only. A custom definition cannot add trusted
speakers or change who is allowed to issue a verified command.

## Exact corrections

Schema v2 may add `corrections.yml`. These are exact normalized full-message
corrections, not glossary substitutions. Source capitalization remains
significant, which prevents a short correction from changing a different call.

```yaml
corrections:
  - source_language: fr
    target_language: en
    scope: global
    source: Ne sautez pas !
    translation: Do not jump!
  - source_language: en
    target_language: de
    scope: pack
    source: Broadcast armor now
    translation: Jetzt Panzerung senden
```

Only `global` and `pack` scopes are accepted. On another pilot's machine, a
`pack` correction binds to that imported pack's own ID and version; a ZIP
cannot overwrite corrections belonging to some other doctrine.

The correction export helper includes global corrections and, optionally, one
active doctrine scope. It emits `corrections.yml` and leaves `terms.yml` empty,
so full chat messages are never reinterpreted as vocabulary fragments.

## Validation limits

Imports reject extra files, nested or traversal paths, symlinks, YAML aliases,
duplicate keys, unsafe command categories, excessive size/counts, and unknown
schema fields. Imported command phrases can create an alert, but only a speaker
explicitly marked FC by the local user can create a verified execution card.
