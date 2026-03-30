Write-Host "=== Webhook Test Script ===" -ForegroundColor Cyan
Write-Host ""

# Check if local server is running
Write-Host "Checking if local server is running..." -ForegroundColor Gray
try {
    $local = Invoke-WebRequest -Uri "http://localhost:8000/health" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    Write-Host "OK Local server is running" -ForegroundColor Green
} catch {
    Write-Host "ERROR Local server is NOT running!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please start the server first:" -ForegroundColor Yellow
    Write-Host "  cd C:\Users\PC\Cutting_Solution_Platform" -ForegroundColor White
    Write-Host "  python main.py" -ForegroundColor White
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit
}

Write-Host ""
Write-Host "Choose test option:" -ForegroundColor Cyan
Write-Host "1. Test with localhost (no ngrok needed)" -ForegroundColor White
Write-Host "2. Test with ngrok URL" -ForegroundColor White
$choice = Read-Host "Enter choice 1 or 2"

if ($choice -eq "2") {
    Write-Host ""
    Write-Host "Enter your ngrok URL" -ForegroundColor Cyan
    Write-Host "Example: https://abc-def-ghi.ngrok-free.app" -ForegroundColor Gray
    $ngrokUrl = Read-Host "Ngrok URL"
    
    if ([string]::IsNullOrWhiteSpace($ngrokUrl)) {
        Write-Host "No URL provided. Using localhost..." -ForegroundColor Yellow
        $ngrokUrl = "http://localhost:8000"
    }
} else {
    $ngrokUrl = "http://localhost:8000"
    Write-Host "Using localhost for testing..." -ForegroundColor Yellow
}

$webhookUrl = "$ngrokUrl/api/webhook/mkg"

Write-Host ""
Write-Host "Testing webhook at: $webhookUrl" -ForegroundColor Cyan
Write-Host ""

$body = @{
    type      = "update_iofa"
    timestamp = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fff")
    data      = @{
        document = 242
        rowkey   = "0x0000000008a0f385"
    }
} | ConvertTo-Json

Write-Host "Sending test payload:" -ForegroundColor Gray
Write-Host $body -ForegroundColor DarkGray
Write-Host ""

try {
    $response = Invoke-WebRequest -Uri $webhookUrl -Method PUT -Headers @{"Content-Type"="application/json"} -Body $body -UseBasicParsing -TimeoutSec 10
    
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "SUCCESS!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "Status Code: $($response.StatusCode)" -ForegroundColor Yellow
    Write-Host "Response:" -ForegroundColor Yellow
    Write-Host $response.Content -ForegroundColor White
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Cyan
    Write-Host "- Check application logs for processing details" -ForegroundColor White
    Write-Host "- View results at: http://localhost:8000" -ForegroundColor White
    Write-Host "- API docs at: http://localhost:8000/docs" -ForegroundColor White
    
} catch {
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "ERROR!" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Yellow
    
    if ($_.Exception.Response) {
        try {
            $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            $responseBody = $reader.ReadToEnd()
            Write-Host "Response Body:" -ForegroundColor Yellow
            Write-Host $responseBody -ForegroundColor White
        } catch {
            Write-Host "Could not read response body" -ForegroundColor Gray
        }
    }
}

Write-Host ""
Read-Host "Press Enter to exit"
