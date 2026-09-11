@echo off
echo ========================================
echo Image to PSD - PyInstaller EXE Builder
echo ========================================
echo.

python --version
if errorlevel 1 (
    echo Python not found in PATH!
    pause
    exit /b 1
)

echo.
echo Installing/Checking PyInstaller...
pip install pyinstaller

echo.
echo Building executable with PyInstaller...
pyinstaller --noconfirm ImageToPSD.spec

if errorlevel 1 (
    echo.
    echo BUILD FAILED! Check the error messages above.
    pause
    exit /b 1
)

echo.
echo ========================================
echo BUILD SUCCESSFUL!
echo Output file: dist\ImageToPSD.exe
echo ========================================
pause
