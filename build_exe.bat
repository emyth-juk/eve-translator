@echo off
echo Fetching version...
for /f "delims=" %%i in ('python -c "from src.version import __version__; print(__version__)"') do set VERSION=%%i
echo Building EVE Fleet Chat Translator v%VERSION%...

pip install -r requirements.txt
pyinstaller --clean --noconfirm --onefile --windowed --name "EVETranslator_v%VERSION%" --icon="src\assets\icon.ico" --add-data "src/assets;src/assets" --add-data "data;data" --collect-data "langdetect" --hidden-import "PySide6" src/main.py

echo Cleaning up potential config leaks in dist...
if exist "dist\translator_config.json" del "dist\translator_config.json"
echo Build Complete. Check dist/EVETranslator_v%VERSION%.exe
pause
