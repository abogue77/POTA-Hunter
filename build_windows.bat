@echo off
echo Checking for PyInstaller...
pip show pyinstaller >nul 2>&1 || pip install pyinstaller
echo.

echo Installing audio and hotkey dependencies...
pip install sounddevice numpy pynput
echo.

echo Generating icon files...
python assets\make_icon.py
echo.

echo Building POTA-Hunter.exe...
pyinstaller --noconfirm POTA-Logger.spec
echo.

echo Downloading rcedit if needed...
if not exist rcedit-x64.exe (
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/electron/rcedit/releases/download/v2.0.0/rcedit-x64.exe' -OutFile 'rcedit-x64.exe'"
)

echo Embedding icon (preserving PyInstaller payload)...
python embed_icon.py
echo.

echo Build complete. Executable is in: dist\POTA-Hunter.exe
pause
