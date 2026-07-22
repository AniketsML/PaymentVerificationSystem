@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  Payment Verification System — one command to run everything (Windows).
REM
REM    run.bat            start the console + workers (+ ingestion if configured)
REM    run.bat --check    preflight checks only
REM    run.bat --port 8010
REM
REM  Uses uv when available (no venv setup needed), else the current Python.
REM ─────────────────────────────────────────────────────────────────────────────
cd /d "%~dp0"
where uv >nul 2>nul
if %ERRORLEVEL%==0 (
    uv run --with-requirements requirements.txt python run.py %*
) else (
    python run.py %*
)
