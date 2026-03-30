Write-Host "Stopping existing services..." -ForegroundColor Yellow

# Stop ngrok
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "✓ Stopped ngrok" -ForegroundColor Green

# Stop Python (uvicorn)
Get-Process python -ErrorAction SilentlyContinue | Where-Object {$_.MainWindowTitle -like "*uvicorn*"} | Stop-Process -Force
Write-Host "✓ Stopped Python server" -ForegroundColor Green

Start-Sleep -Seconds 2

Write-Host "`nStarting services..." -ForegroundColor Cyan

# Start FastAPI
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PSScriptRoot'; python main.py"
Write-Host "✓ Started FastAPI on http://localhost:8000" -ForegroundColor Green

Start-Sleep -Seconds 5

# Start ngrok
Start-Process powershell -ArgumentList "-NoExit", "-Command", "ngrok http 8000"
Write-Host "✓ Started ngrok" -ForegroundColor Green

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "Services started!" -ForegroundColor Green
Write-Host "Check the ngrok window for your public URL" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan
