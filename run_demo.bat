@echo off
echo Starting Eve Fleet Chat Translator Demo...

echo 1. Launching Overlay App...
start "Eve Translator Overlay" cmd /k "python -m src.main"

echo Waiting 5 seconds for app to initialize...
timeout /t 5

REM ============================================================
REM Choose ONE of the following options:
REM ============================================================

REM Option 1: Simulated Fleet (Mixed languages + EVE slang)
echo 2. Running Simulated Fleet...
start "Fleet Simulator" cmd /k "python src/scripts/simulate_fleet.py"

REM Option 2: Real Log Replay (Replay existing log file)
REM echo 2. Running Real Log Replay...
REM start "Fleet Simulator" cmd /k "python src/scripts/replay_real_log_to_file.py"

REM ============================================================

echo Demo running. Look for the transparent overlay window.
echo You can close the terminal windows to stop the demo.
