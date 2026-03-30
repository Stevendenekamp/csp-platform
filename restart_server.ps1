Write-Host "Stopping existing Python processes..." -ForegroundColor Yellow

Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Write-Host "Starting server..." -ForegroundColor Cyan
python main.py
