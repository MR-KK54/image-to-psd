@echo off
setlocal enabledelayedexpansion

echo ========================================================
echo Image to PSD Converter - Local Automated Installer
echo ========================================================
echo.

set "INSTALL_DIR=%LOCALAPPDATA%\Programs\ImageToPSD"
echo Target Installation Directory: !INSTALL_DIR!
echo.

:: Create install directory
if not exist "!INSTALL_DIR!" (
    mkdir "!INSTALL_DIR!"
)

:: Check if source executable exists
if exist "dist\ImageToPSD.exe" (
    set "SOURCE_EXE=dist\ImageToPSD.exe"
) else if exist "ImageToPSD.exe" (
    set "SOURCE_EXE=ImageToPSD.exe"
) else (
    echo ERROR: ImageToPSD.exe not found! Please build first using BUILD_EXE.bat.
    pause
    exit /b 1
)

echo [1/4] Copying application files to !INSTALL_DIR!...
copy /Y "!SOURCE_EXE!" "!INSTALL_DIR!\ImageToPSD.exe" >nul
if exist ".env" copy /Y ".env" "!INSTALL_DIR!\.env" >nul
if exist ".env.example" copy /Y ".env.example" "!INSTALL_DIR!\.env.example" >nul
if exist "index.html" copy /Y "index.html" "!INSTALL_DIR!\index.html" >nul
if exist "groq_matcher.py" copy /Y "groq_matcher.py" "!INSTALL_DIR!\groq_matcher.py" >nul

if exist "templates" xcopy /E /I /Y "templates" "!INSTALL_DIR!\templates" >nul
if exist "static" xcopy /E /I /Y "static" "!INSTALL_DIR!\static" >nul
if exist "app.ico" copy /Y "app.ico" "!INSTALL_DIR!\app.ico" >nul

echo [2/4] Verifying Groq API Internet Connectivity...
curl -s -o nul -w "%%{http_code}" https://api.groq.com/ >temp_status.txt 2>nul
set /p HTTP_STATUS=<temp_status.txt
del temp_status.txt 2>nul
if "!HTTP_STATUS!"=="200" (
    echo  - Groq API reachable over Internet (HTTP 200 OK)
) else (
    echo  - Warning: Groq API response code: !HTTP_STATUS! (Check internet connection)
)

echo [3/4] Creating Desktop and Start Menu Shortcuts...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$wsh = New-Object -ComObject WScript.Shell; $desktop = [System.Environment]::GetFolderPath('Desktop'); $scDesktop = $wsh.CreateShortcut(\"$desktop\Image to PSD.lnk\"); $scDesktop.TargetPath = '%INSTALL_DIR%\ImageToPSD.exe'; $scDesktop.WorkingDirectory = '%INSTALL_DIR%'; $scDesktop.IconLocation = '%INSTALL_DIR%\app.ico'; $scDesktop.Description = 'Image to PSD Converter'; $scDesktop.Save(); $startMenu = [System.Environment]::GetFolderPath('Programs'); $scStart = $wsh.CreateShortcut(\"$startMenu\Image to PSD.lnk\"); $scStart.TargetPath = '%INSTALL_DIR%\ImageToPSD.exe'; $scStart.WorkingDirectory = '%INSTALL_DIR%'; $scStart.IconLocation = '%INSTALL_DIR%\app.ico'; $scStart.Description = 'Image to PSD Converter'; $scStart.Save();"

echo [4/4] Creating Uninstaller script...
(
echo @echo off
echo echo Uninstalling Image to PSD Converter...
echo taskkill /F /IM ImageToPSD.exe 2^>nul
echo rmdir /S /Q "%LOCALAPPDATA%\Programs\ImageToPSD"
echo del "%USERPROFILE%\Desktop\Image to PSD.lnk" 2^>nul
echo del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Image to PSD.lnk" 2^>nul
echo echo Uninstallation Complete.
echo pause
) > "!INSTALL_DIR!\UNINSTALL.bat"

copy /Y "!INSTALL_DIR!\UNINSTALL.bat" "UNINSTALL.bat" >nul

echo.
echo ========================================================
echo INSTALLATION SUCCESSFUL!
echo.
echo Installed to: !INSTALL_DIR!
echo Shortcuts Created:
echo  - Desktop: Image to PSD.lnk
echo  - Start Menu: Image to PSD
echo.
echo Groq AI Access: Internet-enabled via .env configuration.
echo ========================================================
echo.

set /p RUN_NOW="Do you want to launch Image to PSD now? (Y/N): "
if /i "!RUN_NOW!"=="Y" (
    echo Starting Image to PSD Server...
    start "" "!INSTALL_DIR!\ImageToPSD.exe"
)

pause
