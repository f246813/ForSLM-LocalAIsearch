$ErrorActionPreference = 'Stop'
$python = "$PSScriptRoot\runtime\webui-env\Scripts\python.exe"
if (Test-Path -LiteralPath "$PSScriptRoot\runtime\manager.lock") {
    if (-not (Test-Path -LiteralPath $python)) { throw 'The ForSLM runtime Python is missing.' }
    $env:PYTHONUTF8 = '1'
    & $python "$PSScriptRoot\integration\service_manager.py" stop
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$processFile = Join-Path $PSScriptRoot 'runtime\processes.json'
if (-not (Test-Path -LiteralPath $processFile)) { exit }
$records = @(Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json)
[array]::Reverse($records)

function Stop-Tree([System.Diagnostics.Process]$process) {
    if ($process.HasExited) { return }
    foreach ($entry in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($process.Id)")) {
        $child = Get-Process -Id $entry.ProcessId -ErrorAction SilentlyContinue
        if ($child -and -not $child.HasExited -and $entry.ExecutablePath -and
            $child.Path -eq $entry.ExecutablePath -and $entry.CreationDate -ge $process.StartTime -and
            [Math]::Abs(($entry.CreationDate - $child.StartTime).TotalMilliseconds) -lt 1) {
            Stop-Tree $child
        }
    }
    if (-not $process.HasExited) { Stop-Process -InputObject $process }
}

foreach ($record in $records) {
    $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($process -and -not $process.HasExited -and $process.Path -eq $record.executable -and
        $process.StartTime.ToUniversalTime().ToString('o') -eq $record.startTime) {
        Stop-Tree $process
        Write-Host "Stopped $($record.name)"
    }
}
Remove-Item -LiteralPath $processFile
