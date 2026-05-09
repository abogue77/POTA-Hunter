@echo off
echo Checking for PyInstaller...
pip show pyinstaller >nul 2>&1 || pip install pyinstaller
echo.

echo Checking for windows-curses...
pip show windows-curses >nul 2>&1 || pip install windows-curses
echo.

echo Building POTA-Hunter-TUI.exe...
pyinstaller --noconfirm POTA-Hunter-TUI.spec
echo.

echo Build complete. Executable is in: dist\POTA-Hunter-TUI.exe
pause
