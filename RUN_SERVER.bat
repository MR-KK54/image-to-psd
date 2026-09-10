@echo off
REM Robust starter - uses venv if exists, else global Python
echo ========================================
echo Image to PSD - Starting Server
echo ========================================
echo.
REM Prefer venv python if exists
if exist "venv\Scripts\python.exe" (
  echo Found venv, using venv\Scripts\python.exe
  set PYTHON=venv\Scripts\python.exe
) else (
  echo Using global python
  set PYTHON=python
)
%PYTHON% --version
if errorlevel 1 (
  echo Python not found! Install from https://python.org
  echo Make sure to check "Add Python to PATH"
  pause
  exit /b 1
)
echo.
echo Checking dependencies...
%PYTHON% -c "import flask, easyocr, cv2" 2>nul
if errorlevel 1 (
  echo Installing dependencies (first run, 5-10 min)...
  %PYTHON% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Failed to install. Try: %PYTHON% -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)
echo.
echo Starting Flask server...
echo IMPORTANT: WAIT until you see "Running on http://0.0.0.0:5000"
echo Then open in browser address bar (NOT double-click HTML):
echo   http://127.0.0.1:5000  or  http://localhost:5000
echo Test: http://127.0.0.1:5000/api/health  must show {"status":"ok"}
echo.
echo If browser shows "site can't be reached" or "Failed to fetch":
echo  - Do NOT close this window (server dies if closed)
echo  - Wait 20 seconds for EasyOCR model to load
echo  - Try http://127.0.0.1:5000  (not file://)
echo  - Try a different port: %PYTHON% app.py --port 5001  then http://127.0.0.1:5001
echo  - Check firewall: allow python.exe
echo.
echo Press Ctrl+C to stop server
echo ========================================
%PYTHON% app.py --host 0.0.0.0 --port 5000
if errorlevel 1 (
  echo Port 5000 busy, trying 5001...
  %PYTHON% app.py --host 0.0.0.0 --port 5001
)
pause
