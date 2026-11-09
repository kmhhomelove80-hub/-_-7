$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
python web_app.py --smoke-test
if ($LASTEXITCODE -ne 0) { throw 'Local web smoke test failed' }
python tests/web_browser_check.py
if ($LASTEXITCODE -ne 0) { throw 'Browser QA failed' }
python -m PyInstaller --noconfirm --clean --onefile --windowed --name KiwoomWebTrader --add-data 'web;web' --collect-all exchange_calendars --collect-all keyring --hidden-import keyring.backends.Windows web_app.py
if ($LASTEXITCODE -ne 0) { throw 'Web EXE build failed' }
Remove-Item web-smoke-test.json -ErrorAction SilentlyContinue
$process = Start-Process -FilePath "$PWD\dist\KiwoomWebTrader.exe" -ArgumentList '--smoke-test' -Wait -PassThru
if ($process.ExitCode -ne 0) { throw 'Packaged web smoke test failed' }
if (-not (Test-Path web-smoke-test.json)) { throw 'Missing web smoke report' }
$report = Get-Content web-smoke-test.json -Raw | ConvertFrom-Json
if ($report.status -ne 'ok') { throw 'Invalid web smoke report' }
Copy-Item '웹버전_사용설명서.md' dist
Copy-Item web-smoke-test.json dist
Compress-Archive -Path dist\KiwoomWebTrader.exe, dist\웹버전_사용설명서.md, dist\web-smoke-test.json, dist\web-browser-report.json, dist\web-preview.png, dist\schedule-preview.png, dist\strategy-preview.png -DestinationPath dist\KiwoomWebTrader-Windows-x64.zip -Force
Get-ChildItem dist -File | Where-Object { $_.Extension -in '.exe', '.zip' } | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
    "$hash  $($_.Name)"
} | Set-Content -Encoding ascii dist\SHA256SUMS.txt
