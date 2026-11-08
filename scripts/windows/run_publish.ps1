# Runs this week's crawl and publishes the snapshot to the site. Task Scheduler starts it
# (see install_task.ps1); you can also run it by hand to test:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\run_publish.ps1
# Settings and keys come from the project's .env file. Each run writes a UTF-8 log to
# data\logs\ (the 30 most recent are kept).
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("publish-" + (Get-Date -Format "yyyy-MM-dd_HHmm") + ".log")
$python = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = Join-Path $root ".venv\Scripts\python.exe" }
if (-not (Test-Path $python)) {
    "No virtual environment found (venv\ or .venv\). Follow the README quick start first." | Out-File -FilePath $log -Encoding utf8
    exit 1
}
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
& $python -m auction_agent publish *>&1 | ForEach-Object { "$_" } | Out-File -FilePath $log -Encoding utf8
$code = $LASTEXITCODE
"exit code: $code" | Out-File -FilePath $log -Encoding utf8 -Append
Get-ChildItem $logDir -Filter "publish-*.log" | Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 | Remove-Item -Force
exit $code
