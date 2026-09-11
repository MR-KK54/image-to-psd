@echo off
echo Uninstalling Image to PSD Converter...
taskkill /F /IM ImageToPSD.exe 2>nul
rmdir /S /Q "C:\Users\Hxtreme\AppData\Local\Programs\ImageToPSD"
del "C:\Users\Hxtreme\Desktop\Image to PSD.lnk" 2>nul
del "C:\Users\Hxtreme\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Image to PSD.lnk" 2>nul
echo Uninstallation Complete.
pause
