$ErrorActionPreference = "Stop"

$taskName = "Face Attendance Watcher"
$python = "C:\Users\E2NEXT\face-attendance\.venv\Scripts\python.exe"
$workDir = "C:\Users\E2NEXT\face-attendance"

$action = New-ScheduledTaskAction `
  -Execute $python `
  -Argument "-u face_attendance.py watch" `
  -WorkingDirectory $workDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask `
  -TaskName $taskName `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Runs the local RTSP face recognition watcher and creates Frappe Employee Checkin records." `
  -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "Installed and started: $taskName"
