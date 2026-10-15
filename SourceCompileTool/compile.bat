@echo off
setlocal
cd /d "%~dp0"

python -m PyInstaller --noconfirm --clean "CompilePalX.spec"
if errorlevel 1 (
    echo.
    echo PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo Build complete. See the dist folder.
pause
