from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(path, content):
    (ROOT / path).write_text(content, encoding="utf-8")


write(
    "start_face_attendance.ps1",
    r'''$ErrorActionPreference = 'Stop'

$Project = if ($env:FACE_ATTENDANCE_DIR) {
    [System.IO.Path]::GetFullPath($env:FACE_ATTENDANCE_DIR)
} else {
    $PSScriptRoot
}
$Python = Join-Path $Project '.venv\Scripts\python.exe'
$FtpReceiver = Join-Path $Project 'ftp_receiver.py'
$Watcher = Join-Path $Project 'watch_service.py'
$LogDir = Join-Path $Project 'logs'

foreach ($RequiredPath in @($Python, $FtpReceiver, $Watcher)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required file not found: $RequiredPath"
    }
}

function Get-RunningPythonProcess([string] $ScriptName) {
    $Pattern = [regex]::Escape($ScriptName)
    Get-CimInstance Win32_Process -Filter "name = 'python.exe' or name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $Pattern } |
        Select-Object -First 1
}

function Start-PythonBackground([string] $ScriptPath, [string] $LogStem) {
    $QuotedScript = '"' + $ScriptPath + '"'
    Start-Process `
        -FilePath $Python `
        -ArgumentList @('-u', $QuotedScript) `
        -WorkingDirectory $Project `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "$LogStem.out.log") `
        -RedirectStandardError (Join-Path $LogDir "$LogStem.err.log")
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not (Get-RunningPythonProcess 'ftp_receiver.py')) {
    Start-PythonBackground $FtpReceiver 'ftp'
}
if (Get-RunningPythonProcess 'watch_service.py') {
    throw 'The canonical watch_service.py process is already running.'
}

Push-Location $Project
try {
    & $Python -u $Watcher `
        1>> (Join-Path $LogDir 'watch.out.log') `
        2>> (Join-Path $LogDir 'watch.err.log')
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
''',
)

write(
    "start_ftp_only.ps1",
    r'''$ErrorActionPreference = 'Stop'

$Project = if ($env:FACE_ATTENDANCE_DIR) {
    [System.IO.Path]::GetFullPath($env:FACE_ATTENDANCE_DIR)
} else {
    $PSScriptRoot
}
$Python = Join-Path $Project '.venv\Scripts\python.exe'
$FtpReceiver = Join-Path $Project 'ftp_receiver.py'
$Watcher = Join-Path $Project 'watch_service.py'
$LogDir = Join-Path $Project 'logs'

foreach ($RequiredPath in @($Python, $FtpReceiver, $Watcher)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required file not found: $RequiredPath"
    }
}

function Get-RunningPythonProcess([string] $ScriptName) {
    $Pattern = [regex]::Escape($ScriptName)
    Get-CimInstance Win32_Process -Filter "name = 'python.exe' or name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $Pattern } |
        Select-Object -First 1
}

function Start-PythonBackground([string] $ScriptPath, [string] $LogStem) {
    $QuotedScript = '"' + $ScriptPath + '"'
    Start-Process `
        -FilePath $Python `
        -ArgumentList @('-u', $QuotedScript) `
        -WorkingDirectory $Project `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "$LogStem.out.log") `
        -RedirectStandardError (Join-Path $LogDir "$LogStem.err.log")
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not (Get-RunningPythonProcess 'ftp_receiver.py')) {
    Start-PythonBackground $FtpReceiver 'ftp'
}
if (-not (Get-RunningPythonProcess 'watch_service.py')) {
    Start-PythonBackground $Watcher 'watch'
}

Write-Host 'Canonical FTP receiver and watch_service.py are running.'
''',
)

write(
    "install_auto_start.ps1",
    r'''$ErrorActionPreference = 'Stop'

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
''',
)

write(
    "stop_auto_start.ps1",
    r'''$ErrorActionPreference = 'Stop'

$TaskNames = @(
    'Face Attendance Watcher',
    'Face Attendance FTP Receiver'
)

foreach ($TaskName in $TaskNames) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask `
        -TaskName $TaskName `
        -Confirm:$false `
        -ErrorAction SilentlyContinue
    Write-Host "Stopped and removed: $TaskName"
}
''',
)
