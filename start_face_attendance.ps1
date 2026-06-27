$ErrorActionPreference = "Stop"
Set-Location "C:\Users\E2NEXT\face-attendance"

$logDir = Join-Path (Get-Location) "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

& ".\.venv\Scripts\python.exe" -u "face_attendance.py" "watch" `
  1>> (Join-Path $logDir "watch.out.log") `
  2>> (Join-Path $logDir "watch.err.log")
