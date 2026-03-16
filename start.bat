@echo off
setlocal

cd /d "%~dp0"

:: Check for .env file
if not exist ".env" (
    echo ERROR: .env file not found.
    echo Please copy .env.example to .env and fill in your credentials.
    pause
    exit /b 1
)

:: Check for virtual environment
if not exist "venv\Scripts\activate.bat" (
    echo Creating Python virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment. Make sure Python 3.11+ is installed.
        pause
        exit /b 1
    )
)

:: Activate venv
call venv\Scripts\activate.bat

:: Install / upgrade dependencies
echo Installing dependencies...
pip install -r requirements.txt --quiet

:: Launch the app
echo.
echo ============================================================
echo  GC Flow Analyzer is starting...
echo  Open your browser at: http://127.0.0.1:8000
echo  Press Ctrl+C to stop.
echo ============================================================
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

pause
