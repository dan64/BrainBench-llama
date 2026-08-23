@echo off
rem ── BrainBench GUI launcher (Windows) ─────────────────────────
rem Double-click this file (or run it from any directory) to open
rem the benchmark GUI. The window stays open on error so messages
rem are readable; on success it simply closes.
cd /d "%~dp0"
python benchmark\gui.py
if errorlevel 1 (
    echo.
    echo [run_benchmark_gui] The GUI exited with an error (see messages above).
    echo Common fixes:
    echo   - pip install -r benchmark\requirements.txt
    echo   - check that benchmark\config.yaml exists
    pause
)
