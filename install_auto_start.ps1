$ErrorActionPreference = 'Stop'

$Project = if ($env:FACE_ATTENDANCE_DIR) {
    [System.IO.Path]::GetFullPath($env:FACE_ATTENDANCE_DIR)
} else {
    $PSScriptRoot
}
$Python = Join-Path $Project '.venv\Scripts\python.exe'
$FtpReceiver = Join-Path $Project 'ftp_receiver.py'
$Watcher = Join-Path $Project 'watch_service.py'

foreach ($RequiredPath in @($Python, $FtpReceiver, $Watcher)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required file not found: $RequiredPath"
    }
}

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$Tasks = @(
    @{
        Name = 'Face Attendance FTP Receiver'
        Script = $FtpReceiver
        Description = 'Receives staged HOLOWITS FTP captures for the face-attendance node.'
    },
    @{
        Name = 'Face Attendance Watcher'
        Script = $Watcher
        Description = 'Runs the canonical replay-resistant watcher with readiness, PAD, and event-state controls.'
    }
)

foreach ($Task in $Tasks) {
    $ScriptPath = [string] $Task.Script
    $Arguments = "-u `"$ScriptPath`""
    $Action = New-ScheduledTaskAction `
        -Execute $Python `
        -Argument $Arguments `
        -WorkingDirectory $Project

    Register-ScheduledTask `
        -TaskName ([string] $Task.Name) `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description ([string] $Task.Description) `
        -Force | Out-Null

    Start-ScheduledTask -TaskName ([string] $Task.Name)
    Write-Host "Installed and started: $($Task.Name)"
}
