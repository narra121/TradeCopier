# Quick Build Script (No Parameters)
# This is a simplified version of build.ps1 for one-click building

Write-Host "Building Trade Copier Application..." -ForegroundColor Cyan

# Kill any running processes
Get-Process -Name "TradeCopierApp" -ErrorAction SilentlyContinue | Stop-Process -Force

# Clean and build
pyinstaller TradeCopierApp.spec --clean --noconfirm

if ($LASTEXITCODE -eq 0) {
    Write-Host "Build successful!" -ForegroundColor Green
    Write-Host "Executable: .\dist\TradeCopierApp\TradeCopierApp.exe" -ForegroundColor White
} else {
    Write-Host "Build failed!" -ForegroundColor Red
}
