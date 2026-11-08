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

Development is supported and CI-tested on 64-bit Windows with Python 3.11
through 3.15. Release builds use Python 3.15.

1. Clone the repository.
2. Create a virtual environment: `py -3.15 -m venv .venv`.
3. Install the development dependencies: `.venv\Scripts\python -m pip install -r requirements-dev.txt`.
4. Run the app: `.venv\Scripts\python -m src.main`.
5. Run the tests: `.venv\Scripts\python -m pytest`.
6. Run the linter: `.venv\Scripts\python -m ruff check src tests scripts packaging`.

## Maintainer Release Setup

Tagged releases require a protected VirusTotal credential and stop before publication if the scan cannot complete.

1. In the GitHub repository, create an environment named `virustotal-release`.
2. Add an environment secret named `VIRUSTOTAL_API_KEY`. Never add the key to a repository file, command-line argument, cache, artifact, workflow output, or log.
3. Restrict that environment to release tags matching `v*`. For stronger protection, require a reviewer before the scan job can receive the secret.
4. Add a tag ruleset for `v*` that restricts tag creation, updates, and deletion to release maintainers. The workflow also rejects a release tag whose commit is not reachable from `main`.
5. Confirm that using VirusTotal's public API is compatible with the project's current non-commercial use and that the release executable is ready to be shared publicly.

The release workflow keeps its trust boundaries separate:

- **Build** has read-only repository access and no VirusTotal secret.
- **Scan** has read-only access and receives `VIRUSTOTAL_API_KEY` only as a process environment variable. It submits only the packaged EXE, verifies its hash against `BUNDLED_COMPONENTS.json`, waits for completed aggregate findings, and writes fixed-format notes.
- **Publish** has release-write access but no VirusTotal secret. A result with zero malicious and zero suspicious classifications publishes normally; any such classification creates a draft release for review. Missing credentials, API failures, malformed results, hash mismatches, and timeouts prevent release creation.

Standard VirusTotal uploads and reports are shared with its security community and partners. Never adapt this job to upload configuration, logs, user data, API keys, private prereleases, or source bundles. See VirusTotal's [submission model](https://docs.virustotal.com/docs/how-it-works), [public API restrictions](https://docs.virustotal.com/reference/public-vs-premium-api), and [report-link format](https://docs.virustotal.com/docs/most-recent-report).

## License
By contributing, you agree that your contributions will be licensed under its MIT License.
