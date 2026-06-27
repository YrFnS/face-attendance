$ErrorActionPreference = 'Stop'

$Project = 'C:\Users\E2NEXT\face-attendance'
$Python = Join-Path $Project '.venv\Scripts\python.exe'

function Test-Running($Pattern) {
    Get-CimInstance Win32_Process -Filter "name = 'python.exe' or name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -match $Pattern } |
        Select-Object -First 1
}

if (-not (Test-Running 'ftp_receiver\.py')) {
    Start-Process -FilePath $Python -ArgumentList '-u', 'ftp_receiver.py' -WorkingDirectory $Project -WindowStyle Hidden
}

if (-not (Test-Running 'face_attendance\.py\s+watch-folder')) {
    Start-Process -FilePath $Python -ArgumentList '-u', 'face_attendance.py', 'watch-folder' -WorkingDirectory $Project -WindowStyle Hidden
}
