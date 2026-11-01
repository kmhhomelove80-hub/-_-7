$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
python app.py --smoke-test
if ($LASTEXITCODE -ne 0) { throw 'Source UI smoke test failed' }
python -m PyInstaller --noconfirm --clean --onefile --windowed --name KiwoomTwoStockTrader --collect-all exchange_calendars --collect-all keyring --hidden-import keyring.backends.Windows app.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
Remove-Item smoke-test.json -ErrorAction SilentlyContinue
$process = Start-Process -FilePath "$PWD\dist\KiwoomTwoStockTrader.exe" -ArgumentList '--smoke-test' -Wait -PassThru
if ($process.ExitCode -ne 0) { throw 'Packaged UI smoke test failed' }
if (-not (Test-Path smoke-test.json)) { throw 'Missing packaged smoke test report' }
$report = Get-Content smoke-test.json -Raw | ConvertFrom-Json
if ($report.status -ne 'ok') { throw 'Invalid smoke test result' }
Copy-Item '사용설명서.md' dist
Copy-Item smoke-test.json dist
Compress-Archive -Path dist\KiwoomTwoStockTrader.exe, dist\사용설명서.md, dist\smoke-test.json -DestinationPath dist\KiwoomTwoStockTrader-Windows-x64.zip -Force
Get-ChildItem dist -File | Where-Object { $_.Extension -in '.exe', '.zip' } | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
    "$hash  $($_.Name)"
} | Set-Content -Encoding ascii dist\SHA256SUMS.txt
