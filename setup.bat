@echo off
REM Quick setup script for Mobile UI to PSD Converter

echo.
echo ========================================
echo Mobile UI to PSD Converter - Setup
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found!
    echo.
    echo Please install Python from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation
    pause
    exit /b 1
)

echo ✓ Python found
echo.

REM Check if venv exists
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    echo ✓ Virtual environment created
) else (
    echo ✓ Virtual environment already exists
)

echo.
echo Activating virtual environment...
call venv\Scripts\activate.bat

echo.
echo Installing dependencies...
echo This may take 5-10 minutes (downloading models)...
pip install -r requirements.txt

if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)

echo.
echo ✓ Dependencies installed successfully!
echo.
echo ========================================
echo Setup Complete! Starting server...
echo ========================================
echo.
echo Server will start at: http://localhost:5000
echo Open this URL in your web browser
echo.
echo Press Ctrl+C in terminal to stop the server
echo.

python app.py
