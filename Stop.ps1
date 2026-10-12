$ErrorActionPreference = 'Stop'
$processFile = Join-Path $PSScriptRoot 'runtime\processes.json'
if (-not (Test-Path -LiteralPath $processFile)) { exit }
$records = @(Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json)
[array]::Reverse($records)
function Stop-Tree([int]$processId) {
    # Virtualenv and console-script launchers create child Python processes on Windows.
    foreach ($descendant in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $processId")) {
        Stop-Tree $descendant.ProcessId
    }
    Stop-Process -Id $processId -ErrorAction SilentlyContinue
}
foreach ($record in $records) {
    $child = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($child -and $child.StartTime.ToUniversalTime().ToString('o') -eq $record.startTime -and $child.Path -eq $record.executable) {
        Stop-Tree $child.Id
        Write-Host "Stopped $($record.name)"
    }
}
Remove-Item -LiteralPath $processFile
