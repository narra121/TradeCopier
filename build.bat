@echo off
REM Windows Batch file for building Trade Copier
REM Alternative to PowerShell script for environments where PS execution is restricted

echo ============================================================
echo Trade Copier Application Builder (Batch)
echo ============================================================

REM Check if main.py exists
if not exist "main.py" (
    echo Error: main.py not found. Please run from Trade Copier root directory.
    pause
    exit /b 1
)

REM Check if spec file exists
if not exist "TradeCopierApp.spec" (
    echo Error: TradeCopierApp.spec not found.
    pause
    exit /b 1
)

echo Stopping any running TradeCopierApp processes...
taskkill /f /im TradeCopierApp.exe 2>nul

echo Cleaning previous builds...
if exist "dist" rmdir /s /q "dist" 2>nul
if exist "build" rmdir /s /q "build" 2>nul

echo Starting PyInstaller build...
pyinstaller TradeCopierApp.spec --clean --noconfirm

if %errorlevel% equ 0 (
    echo.
    echo ============================================================
    echo BUILD SUCCESSFUL!
    echo ============================================================
    echo Executable created: .\dist\TradeCopierApp\TradeCopierApp.exe
    echo.
    echo To run: .\dist\TradeCopierApp\TradeCopierApp.exe
    echo.
) else (
    echo.
    echo ============================================================
    echo BUILD FAILED!
    echo ============================================================
    echo PyInstaller returned error code %errorlevel%
    echo.
)

pause
