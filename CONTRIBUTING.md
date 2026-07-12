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

Development is supported and CI-tested on 64-bit Windows with Python 3.10,
3.11, and 3.12. Release builds use Python 3.10.

1. Clone the repository.
2. Create a virtual environment: `py -3.10 -m venv .venv`.
3. Install the development dependencies: `.venv\Scripts\python -m pip install -r requirements-dev.txt`.
4. Run the app: `.venv\Scripts\python -m src.main`.
5. Run the tests: `.venv\Scripts\python -m pytest`.
6. Run the linter: `.venv\Scripts\python -m ruff check src tests scripts packaging`.

## License
By contributing, you agree that your contributions will be licensed under its MIT License.
