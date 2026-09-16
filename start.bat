@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv not found.
    echo Install with: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo Then reopen the command prompt and try again.
    pause
    exit /b 1
)

uv run main.py
set "EXIT=%ERRORLEVEL%"
if not "%EXIT%"=="0" (
    echo.
    echo Program exited with code %EXIT%.
    pause
)
endlocal
