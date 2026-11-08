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

rem Release builds embed Python 3.15; run this from a Python 3.15 environment.
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 15) else 1)" || (
    echo Release builds use Python 3.15. Activate a Python 3.15 virtual environment first.
    exit /b 1
)

python -m pip install --upgrade pip==26.3 || exit /b 1
python -m pip install -r requirements-build.txt || exit /b 1
python -m PyInstaller --clean --noconfirm --onefile --windowed ^
  --name "EVETranslator_v%VERSION%" ^
  --icon "src\assets\icon.ico" ^
  --version-file "packaging\windows_version_info.txt" ^
  --additional-hooks-dir "packaging\hooks" ^
  --add-data "src\assets\icon.png;src\assets" ^
  --add-data "data;data" ^
  --collect-data "langdetect" ^
  --hidden-import "PySide6" ^
  --exclude-module "PySide6.QtNetwork" ^
  --exclude-module "setuptools" ^
  --exclude-module "packaging" ^
  --exclude-module "wheel" ^
  --exclude-module "typing_extensions" ^
  src\main.py || exit /b 1

python scripts\package_windows_release.py ^
  --executable "dist\EVETranslator_v%VERSION%.exe" ^
  --version "%VERSION%" || exit /b 1

echo Build Complete. Release: dist\EVETranslator_v%VERSION%_windows_x64.zip
pause
