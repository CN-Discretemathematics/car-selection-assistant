# Register a Windows Scheduled Task: daily 08:30 auto-fetch of last month's sales.
# The fetcher (tools\fetch_sales_scheduled.py) is idempotent: if the portal has not
# published last month's data yet, it logs "not ready" and retries next day.
# Usage (from backend dir): powershell -ExecutionPolicy Bypass -File tools\register_sales_task.ps1
# NOTE: this file is intentionally ASCII-only (PowerShell 5.1 reads scripts as ANSI without BOM).
$ErrorActionPreference = "Stop"

$backend = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "python/py not found in PATH" }

$logDir = Join-Path $backend "logs"
$log = Join-Path $logDir "sales_fetch_task.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Inner command: cd backend, run the fetcher, append all output to the log file.
$inner = "Set-Location '$backend'; & '$python' tools\fetch_sales_scheduled.py *>> '$log'"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -Command `"$inner`""
$trigger = New-ScheduledTaskTrigger -Daily -At 8:30AM
Register-ScheduledTask -TaskName "carSelection-sales-fetch" -Action $action -Trigger $trigger -Force

Write-Host "Registered daily 08:30 task 'carSelection-sales-fetch':"
Write-Host "  Log: $log"
Get-ScheduledTask -TaskName "carSelection-sales-fetch" | Select-Object TaskName, State
(Get-ScheduledTaskInfo -TaskName "carSelection-sales-fetch") | Select-Object NextRunTime, LastRunTime, LastTaskResult
Write-Host "Run once now: Start-ScheduledTask -TaskName `"carSelection-sales-fetch`""
Write-Host "Remove:        Unregister-ScheduledTask -TaskName `"carSelection-sales-fetch`" -Confirm:`$false"
