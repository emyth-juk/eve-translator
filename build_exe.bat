@echo off
setlocal
cd /d "%~dp0"

echo Fetching version...
for /f "delims=" %%i in ('python -c "from src.version import __version__; print(__version__)"') do set VERSION=%%i
if not defined VERSION (
    echo Failed to determine application version.
    exit /b 1
)

echo Building EVE Fleet Chat Translator v%VERSION%...

python -m pip install -r requirements-build.txt || exit /b 1
python -m PyInstaller --clean --noconfirm --onefile --windowed ^
  --name "EVETranslator_v%VERSION%" ^
  --icon "src\assets\icon.ico" ^
  --add-data "src\assets\icon.png;src\assets" ^
  --add-data "data;data" ^
  --collect-data "langdetect" ^
  --hidden-import "PySide6" ^
  src\main.py || exit /b 1

echo Build Complete. Check dist\EVETranslator_v%VERSION%.exe
pause
