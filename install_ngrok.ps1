Write-Host "Installing ngrok..." -ForegroundColor Green

# Download ngrok
$ngrokUrl = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"
$outputPath = "$env:TEMP\ngrok.zip"
$extractPath = "C:\Users\PC\ngrok"

Write-Host "Downloading ngrok..."
Invoke-WebRequest -Uri $ngrokUrl -OutFile $outputPath

Write-Host "Extracting to $extractPath..."
New-Item -ItemType Directory -Force -Path $extractPath | Out-Null
Expand-Archive -Path $outputPath -DestinationPath $extractPath -Force

Write-Host "Cleaning up..."
Remove-Item $outputPath

Write-Host ""
Write-Host "✓ Ngrok installed to: $extractPath" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "1. Get your authtoken from: https://dashboard.ngrok.com/get-started/your-authtoken"
Write-Host "2. Run: C:\Users\PC\ngrok\ngrok.exe config add-authtoken YOUR_TOKEN"
Write-Host "3. Run: C:\Users\PC\ngrok\ngrok.exe http 8000"
