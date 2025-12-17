# EVE Translator


<img src="src/assets/icon.png" width="100" alt="icon">

A real-time, overlay-based chat translator for EVE Online, specifically designed to translate Chinese (and other languages) fleet and local chat into English.


## Features

*   **Dual Overlay Windows**: Separate configurable overlays for **Fleet** and **Local** chat.
*   **Smart EVE Glossary**: automatically translates common EVE slang, ship names, and fleet commands (e.g., `毒蜥` -> `Gila`, `锚定` -> `Anchor up`) *before* machine translation.
*   **DeepL Integration**: High-quality neural translation using the DeepL API (Free tier supported).
*   **Robust Detection**: Specifically tuned to handle EVE-specific Chinese shorthand, avoiding common false positives (like Korean/Swahili detections).
*   **Noise Filtering**: Ignores item spam (`Capital Abyssal Armor Repairer`), contracts, and HyperNet offers.
*   **Custom Glossaries**: Support for user-defined terms in `%USERPROFILE%\.eve_translator\glossaries\` to override built-in translations.
*   **Smart Fleet Switching**: Automatically detects active fleets and switches to the correct log file. Includes support for "Back-to-Back" fleets (re-fleeting with same character) and ignores stale logs (>30m inactive).
*   **Stale Log Filtering**: Prevents confusion by refusing to load old message history from inactive logs.
*   **Configurable Backfill**: Choose how much message history (0-50 lines) to load when joining a fleet (Default: 5 lines).
*   **Customizable UI**: Adjust opacity, font size, and colors directly from the settings menu.
*   **Context Menu Enhancements**: Fleet logs display start times [HH:MM] for easy identification.

## Installation

### Quick Start (Pre-built)

1.  Download the latest release from the [Releases Page](https://github.com/emyth-juk/eve-translator/releases).
2.  Extract the `.zip` archive.
3.  Run `EVETranslator_vX.X.X.exe`.
4.  Optional: Configure your DeepL API key in `translator_config.json` (created on first run).

### Development Prerequisites

1.  **Python 3.10+** installed.
2.  **DeepL API Key** (Free).

### Setup

1.  Clone the repository:
    ```bash
    git clone https://github.com/emyth-juk/eve-translator.git
    cd eve-translator
    ```

2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

3.  **Get a DeepL API Key**:
    *   Go to [DeepL API Signup](https://www.deepl.com/pro-api).
    *   Sign up for the **DeepL API Free** plan.
    *   Go to your Account Management -> API Keys.
    *   Copy your key (it usually ends in `:fx` for free tier).

4.  **Configuration**:
    *   Copy `translator_config.example.json` to `translator_config.json`.
    *   Open `translator_config.json` and paste your API key into `"deepl_api_key"`.

## Usage

Run the main script:
```bash
python -m src.main
```

1.  Dimensions and positions of the overlays can be adjusted by dragging the window edges.
2.  Right-click any overlay to access **Settings**, **Export Logs**, or **Toggle Sessions**.
3.  **Local Chat**: Ensure your EVE client has "Log Chat to File" enabled in Settings -> Chat.

## Logs & Troubleshooting

Technical log files are stored in:
`%USERPROFILE%\.eve_translator\logs`

Please include relevant logs when reporting issues.

## Language Configuration

You can configure the **Source** and **Target** languages in `translator_config.json` using **ISO 639-1** two-letter codes.

- **Common Codes**:
  - `en` (English), `zh` (Chinese), `de` (German), `fr` (French), `ru` (Russian), `ja` (Japanese), `ko` (Korean).

- **Full List**: [Wikipedia ISO 639-1 Code List](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes).

## Building from Source (Windows EXE)

To build a standalone `.exe` (Windows):

1.  Run the build script:
    ```cmd
    build_exe.bat
    ```
2.  The executable will be in the `dist/` folder.

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.
