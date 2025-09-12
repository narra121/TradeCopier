# Trade Copier Build Script
# This script builds the Trade Copier application using PyInstaller

param(
    [switch]$Clean,
    [switch]$NoConfirm,
    [string]$OutputDir = "dist"
)

Write-Host "=" * 60 -ForegroundColor Cyan
Write-Host "Trade Copier Application Builder" -ForegroundColor Cyan
Write-Host "=" * 60 -ForegroundColor Cyan

# Set error action preference
$ErrorActionPreference = "Stop"

try {
    # Check if we're in the correct directory
    if (-not (Test-Path "main.py")) {
        Write-Error "main.py not found. Please run this script from the Trade Copier root directory."
        exit 1
    }

    # Check if PyInstaller is installed
    Write-Host "Checking PyInstaller installation..." -ForegroundColor Yellow
    try {
        pyinstaller --version | Out-Null
        Write-Host "✓ PyInstaller found" -ForegroundColor Green
    }
    catch {
        Write-Error "PyInstaller not found. Please install it with: pip install pyinstaller"
        exit 1
    }

    # Check if spec file exists
    if (-not (Test-Path "TradeCopierApp.spec")) {
        Write-Error "TradeCopierApp.spec not found in current directory."
        exit 1
    }

    # Kill any running TradeCopierApp processes to avoid file locks
    Write-Host "Checking for running TradeCopierApp processes..." -ForegroundColor Yellow
    $runningProcesses = Get-Process -Name "TradeCopierApp" -ErrorAction SilentlyContinue
    if ($runningProcesses) {
        Write-Host "Found running TradeCopierApp processes. Attempting to stop them..." -ForegroundColor Yellow
        $runningProcesses | Stop-Process -Force
        Start-Sleep -Seconds 2
        Write-Host "✓ Stopped running processes" -ForegroundColor Green
    } else {
        Write-Host "✓ No running processes found" -ForegroundColor Green
    }

    # Clean previous builds if requested or if dist folder is locked
    if ($Clean -or (Test-Path $OutputDir)) {
        Write-Host "Cleaning previous build artifacts..." -ForegroundColor Yellow
        try {
            if (Test-Path $OutputDir) {
                Remove-Item -Path $OutputDir -Recurse -Force
                Write-Host "✓ Removed $OutputDir directory" -ForegroundColor Green
            }
            if (Test-Path "build") {
                Remove-Item -Path "build" -Recurse -Force
                Write-Host "✓ Removed build directory" -ForegroundColor Green
            }
        }
        catch {
            Write-Warning "Could not remove some build artifacts. They may be in use."
            Write-Host "Attempting to continue..." -ForegroundColor Yellow
        }
    }

    # Build the application
    Write-Host "Starting PyInstaller build process..." -ForegroundColor Yellow
    Write-Host "This may take several minutes..." -ForegroundColor Gray

    $pyinstallerArgs = @("TradeCopierApp.spec")
    
    if ($Clean) {
        $pyinstallerArgs += "--clean"
    }
    
    if ($NoConfirm) {
        $pyinstallerArgs += "--noconfirm"
    }

    # Run PyInstaller
    $startTime = Get-Date
    & pyinstaller @pyinstallerArgs

    if ($LASTEXITCODE -eq 0) {
        $endTime = Get-Date
        $buildTime = $endTime - $startTime
        
        Write-Host "=" * 60 -ForegroundColor Green
        Write-Host "✓ BUILD SUCCESSFUL!" -ForegroundColor Green
        Write-Host "=" * 60 -ForegroundColor Green
        Write-Host "Build time: $($buildTime.ToString('mm\:ss'))" -ForegroundColor Green
        Write-Host ""
        Write-Host "Output location:" -ForegroundColor Cyan
        Write-Host "  $((Get-Location).Path)\$OutputDir\TradeCopierApp\TradeCopierApp.exe" -ForegroundColor White
        Write-Host ""
        Write-Host "To run with production config:" -ForegroundColor Cyan
        Write-Host "  .\$OutputDir\TradeCopierApp\TradeCopierApp.exe" -ForegroundColor White
        Write-Host ""
        Write-Host "To run with custom config:" -ForegroundColor Cyan
        Write-Host "  .\$OutputDir\TradeCopierApp\TradeCopierApp.exe --config config\config_dev.json" -ForegroundColor White
        Write-Host ""
        
        # Check if executable was created
        $exePath = "$OutputDir\TradeCopierApp\TradeCopierApp.exe"
        if (Test-Path $exePath) {
            $fileInfo = Get-Item $exePath
            Write-Host "Executable size: $([math]::Round($fileInfo.Length / 1MB, 2)) MB" -ForegroundColor Gray
            Write-Host "Created: $($fileInfo.CreationTime)" -ForegroundColor Gray
        }
        
    } else {
        Write-Host "=" * 60 -ForegroundColor Red
        Write-Host "✗ BUILD FAILED!" -ForegroundColor Red
        Write-Host "=" * 60 -ForegroundColor Red
        Write-Host "PyInstaller exited with code: $LASTEXITCODE" -ForegroundColor Red
        Write-Host ""
        Write-Host "Common solutions:" -ForegroundColor Yellow
        Write-Host "  1. Close any running TradeCopierApp.exe processes" -ForegroundColor White
        Write-Host "  2. Run with -Clean parameter to remove old builds" -ForegroundColor White
        Write-Host "  3. Check the build log above for specific errors" -ForegroundColor White
        Write-Host "  4. Ensure all required dependencies are installed" -ForegroundColor White
        exit $LASTEXITCODE
    }

} catch {
    Write-Host "=" * 60 -ForegroundColor Red
    Write-Host "✗ BUILD ERROR!" -ForegroundColor Red
    Write-Host "=" * 60 -ForegroundColor Red
    Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "Stack trace:" -ForegroundColor Yellow
    Write-Host $_.ScriptStackTrace -ForegroundColor Gray
    exit 1
}

Write-Host ""
Write-Host "Build script completed." -ForegroundColor Cyan
