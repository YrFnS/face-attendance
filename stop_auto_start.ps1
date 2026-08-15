$ErrorActionPreference = 'Stop'

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
