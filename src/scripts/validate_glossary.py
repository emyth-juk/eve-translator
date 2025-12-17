"""
Glossary Validation Tool
Checks zh_en.yml and zh_de.yml for common issues and provides a quality report.
"""

import yaml
import sys
from pathlib import Path
from collections import defaultdict
import re


class GlossaryValidator:
    def __init__(self, glossary_path):
        self.path = Path(glossary_path)
        self.errors = []
        self.warnings = []
        self.info = []
        self.data = None
        self.flat_entries = {}

    def load(self):
        """Load and parse YAML file."""
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                self.data = yaml.safe_load(f)
            return True
        except yaml.YAMLError as e:
            self.errors.append(f"YAML Syntax Error: {e}")
            return False
        except FileNotFoundError:
            self.errors.append(f"File not found: {self.path}")
            return False

    def flatten_dict(self, d, parent_key=''):
        """Recursively flatten nested dictionary."""
        items = {}
        for k, v in d.items():
            if isinstance(v, dict):
                items.update(self.flatten_dict(v, parent_key))
            else:
                items[k] = v
        return items

    def validate_structure(self):
        """Check YAML structure."""
        if not self.data:
            self.errors.append("Empty or invalid YAML file")
            return

        # Check for meta section
        if 'meta' not in self.data:
            self.warnings.append("Missing 'meta' section with version info")
        else:
            meta = self.data['meta']
            required_meta = ['source_lang', 'target_lang', 'version']
            for key in required_meta:
                if key not in meta:
                    self.warnings.append(f"Missing meta.{key}")

        # Flatten for entry validation
        data_copy = {k: v for k, v in self.data.items() if k != 'meta'}
        self.flat_entries = self.flatten_dict(data_copy)

        self.info.append(f"Total entries: {len(self.flat_entries)}")

    def validate_duplicates(self):
        """Check for duplicate keys and values."""
        # Check duplicate Chinese terms (keys)
        key_counts = defaultdict(int)
        for key in self.flat_entries.keys():
            key_counts[key] += 1

        duplicates = {k: v for k, v in key_counts.items() if v > 1}
        if duplicates:
            for key, count in duplicates.items():
                self.errors.append(f"Duplicate Chinese term: '{key}' appears {count} times")

        # Check duplicate English translations (values)
        value_to_keys = defaultdict(list)
        for key, value in self.flat_entries.items():
            # Normalize value (remove inline comments)
            clean_value = value.split('#')[0].strip() if isinstance(value, str) else str(value)
            value_to_keys[clean_value].append(key)

        # Report values with multiple Chinese terms
        multi_translations = {v: k for v, k in value_to_keys.items() if len(k) > 1}
        if multi_translations:
            self.info.append(f"\nTerms with multiple Chinese variations ({len(multi_translations)}):")
            for eng, chi_list in sorted(multi_translations.items()):
                chi_str = ', '.join(chi_list)
                self.info.append(f"  '{eng}' ← [{chi_str}]")

    def validate_mixed_languages(self):
        """Check for English keys (should be Chinese only)."""
        english_pattern = re.compile(r'^[a-zA-Z0-9\s\-_/()]+$')

        for key in self.flat_entries.keys():
            key_str = str(key)  # Convert to string in case of numeric keys
            if english_pattern.match(key_str):
                self.warnings.append(f"English key found (should be Chinese): '{key}' → '{self.flat_entries[key]}'")

    def validate_empty_values(self):
        """Check for empty or None values."""
        for key, value in self.flat_entries.items():
            if not value or (isinstance(value, str) and not value.strip()):
                self.errors.append(f"Empty translation: '{key}' has no value")

    def validate_ambiguous_translations(self):
        """Check for ambiguous translations (multiple translations separated by /)."""
        for key, value in self.flat_entries.items():
            if isinstance(value, str) and '/' in value:
                # Split and check if they're significantly different
                parts = [p.strip() for p in value.split('/')]
                if len(parts) > 1:
                    self.warnings.append(f"Ambiguous translation: '{key}' → '{value}' (multiple options)")

    def validate_special_characters(self):
        """Check for special characters that might cause issues."""
        for key, value in self.flat_entries.items():
            # Check for quotes that might break parsing
            if isinstance(value, str):
                if '"' in value or "'" in value:
                    self.warnings.append(f"Quotes in translation: '{key}' → '{value}'")

                # Check for tabs or unusual whitespace
                if '\t' in value or '  ' in value:
                    self.warnings.append(f"Extra whitespace in: '{key}' → '{value}'")

    def validate_consistency(self):
        """Check for consistency issues."""
        # Check for inconsistent capitalization of same term
        value_variations = defaultdict(set)
        for key, value in self.flat_entries.items():
            if isinstance(value, str):
                normalized = value.lower().strip()
                value_variations[normalized].add(value)

        inconsistent = {k: v for k, v in value_variations.items() if len(v) > 1}
        if inconsistent:
            self.info.append(f"\nInconsistent capitalization ({len(inconsistent)}):")
            for normalized, variations in sorted(inconsistent.items()):
                if len(variations) > 1:
                    var_str = ', '.join(f"'{v}'" for v in variations)
                    self.info.append(f"  {var_str}")

    def validate_tautologies(self):
        """Check for entries where key equals value (pointless entries)."""
        for key, value in self.flat_entries.items():
            key_str = str(key).strip().lower()
            value_str = str(value).strip().lower() if value else ""
            if key_str == value_str:
                self.warnings.append(f"Tautology: '{key}' → '{value}' (key = value)")

    def run_all_validations(self):
        """Run all validation checks."""
        print(f"\n{'='*60}")
        print(f"Validating: {self.path.name}")
        print(f"{'='*60}\n")

        if not self.load():
            self.print_report()
            return False

        self.validate_structure()
        self.validate_duplicates()
        self.validate_mixed_languages()
        self.validate_empty_values()
        self.validate_ambiguous_translations()
        self.validate_special_characters()
        self.validate_consistency()
        self.validate_tautologies()

        self.print_report()
        return len(self.errors) == 0

    def print_report(self):
        """Print validation report."""
        # Errors
        if self.errors:
            print(f"\n[!] ERRORS ({len(self.errors)}):")
            for err in self.errors:
                print(f"  - {err}")

        # Warnings
        if self.warnings:
            print(f"\n[*] WARNINGS ({len(self.warnings)}):")
            for warn in self.warnings:
                print(f"  - {warn}")

        # Info
        if self.info:
            print(f"\n[i] INFO:")
            for info in self.info:
                print(f"  {info}")

        # Summary
        print(f"\n{'='*60}")
        if not self.errors and not self.warnings:
            print("[OK] Glossary is valid!")
        elif not self.errors:
            print(f"[OK] No errors, but {len(self.warnings)} warnings to review")
        else:
            print(f"[FAIL] Found {len(self.errors)} errors and {len(self.warnings)} warnings")
        print(f"{'='*60}\n")


def main():
    """Validate all glossary files."""
    base_dir = Path(__file__).parent.parent.parent / 'data' / 'glossaries'

    glossaries = [
        base_dir / 'zh_en.yml',
        base_dir / 'zh_de.yml'
    ]

    all_valid = True
    for glossary_path in glossaries:
        if glossary_path.exists():
            validator = GlossaryValidator(glossary_path)
            is_valid = validator.run_all_validations()
            all_valid = all_valid and is_valid
        else:
            print(f"⚠️  Glossary not found: {glossary_path}")

    return 0 if all_valid else 1


if __name__ == "__main__":
    sys.exit(main())
