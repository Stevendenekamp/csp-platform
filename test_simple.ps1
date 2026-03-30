$body = '{"event_type":"order.created","order_id":"TEST-001","data":{"articleCode":"MAT-6000","stockLength":6000}}'

Write-Host "Testing localhost webhook..." -ForegroundColor Cyan

try {
    $response = Invoke-WebRequest -Uri "http://localhost:8000/api/webhook/mkg" -Method POST -ContentType "application/json" -Body $body -UseBasicParsing
    Write-Host "SUCCESS! Status: $($response.StatusCode)" -ForegroundColor Green
    Write-Host $response.Content
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
}
