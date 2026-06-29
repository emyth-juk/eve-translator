@echo off
echo Starting Eve Fleet Chat Translator Demo...

echo 1. Launching Overlay App...
start "Eve Translator Overlay" cmd /k "python -m src.main"

echo Waiting 5 seconds for app to initialize...
timeout /t 5

echo 2. Running Simulated Fleet...
start "Fleet Simulator" cmd /k "python src/scripts/simulate_fleet.py"

echo Demo running. Look for the transparent overlay window.
echo You can close the terminal windows to stop the demo.
