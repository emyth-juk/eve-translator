# Contributing to EVE Fleet Chat Translator

Thank you for your interest in contributing! This project relies on community knowledge to keep EVE Online terminology accurate.

## How to Contribute

### 1. Glossary Updates
The most common way to contribute is by adding or correcting EVE terms in the glossary.

- **Location**: `data/glossaries/`
- **Files**: 
    - `zh_en.yml`: Chinese to English
    - `zh_de.yml`: Chinese to German
- **Format**: See `data/glossaries/README.md` for detailed instructions on structure and categories.

**Tip**: You can use the `debug_lang_items.py` script to test specific terms locally before submitting.

### 2. Code Contributions
- **Bug Fixes**: Open an issue first describing the bug.
- **New Features**: Please discuss in an issue before starting major work.
- **Style**: We use standard Python PEP 8.

### 3. Pull Request Process
1.  Fork the repository.
2.  Create a feature branch (`git checkout -b feature/AmazingFeature`).
3.  Commit your changes (`git commit -m 'Add some AmazingFeature'`).
4.  Push to the branch (`git push origin feature/AmazingFeature`).
5.  Open a Pull Request.

## Development Setup

1.  Clone repo.
2.  Install requirements: `pip install -r requirements.txt`.
3.  Run `python -m src.main`.

## License
By contributing, you agree that your contributions will be licensed under its MIT License.
