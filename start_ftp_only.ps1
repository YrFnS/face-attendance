$ErrorActionPreference = 'Stop'

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
