# Registers the weekly crawl in Windows Task Scheduler: Thursday 19:17 and Saturday 09:43
# (the PC's local time, which should be Paris time). If the PC was off at that moment, the
# task runs as soon as it is back on. Run once, in PowerShell, from the project folder:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_task.ps1
# Remove it later with:  Unregister-ScheduledTask -TaskName "Auction Agent weekly crawl"
$taskName = "Auction Agent weekly crawl"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$runner = Join-Path $PSScriptRoot "run_publish.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`"" -WorkingDirectory $root
$triggers = @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Thursday -At "19:17"),
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At "09:43")
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Auction Agent: crawl this week's Alcopa Beauvais sale, score it and publish it to the site." -Force | Out-Null
Write-Host "Registered '$taskName' (Thursday 19:17 and Saturday 09:43)."
Write-Host "Run it once now to test:  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Logs are written to $root\data\logs\"
