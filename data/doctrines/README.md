# Doctrine pack format

A doctrine pack is a flat ZIP containing exactly four UTF-8, safe-YAML files.
Import it from **Settings → Translation Intelligence → Import ZIP**. One pack
can be active at a time. Packs are data-only and cannot nominate trusted FC or
scout characters.

`manifest.yml`:

```yaml
schema_version: 1
id: armor-hacs
name: Armor HACs
version: 1.0.0
description: Alliance terminology for the armor wing.
```

`terms.yml`:

```yaml
pairs:
  zh_en:
    守门: Hold the gate
  de->en:
    Ziel: Target
```

`protected_entities.yml`:

```yaml
entities:
  - X47L-Q
  - Baltec Megathron
```

`commands.yml` contains literal complete phrases mapped to the app's existing
command codes. Values are never treated as regular expressions:

```yaml
commands:
  ALIGN:
    - form on the outgate
  CEASE_FIRE:
    - red cycle
```

Supported codes are `HOLD`, `DO_NOT_JUMP`, `JUMP`, `ALIGN`, `WARP`, `ANCHOR`,
`FREE_BURN`, `PRIMARY`, `SWITCH_PRIMARY`, `LOCK`, `CEASE_FIRE`, `PROP_ON`,
`PROP_OFF`, `OVERHEAT`, `BUBBLE_UP`, `BUBBLE_DOWN`, `REGROUP`, and
`STAR_BURST`.

Imports reject extra files, nested or traversal paths, symlinks, YAML aliases,
duplicate keys, unsupported command codes, excessive size/counts, and unknown
schema fields. Imported command phrases can create an alert, but only a speaker
explicitly marked FC can create a verified execution card.
