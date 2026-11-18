param([switch]$NoBrowser, [string]$ManagedBrowser = '')
$ErrorActionPreference = 'Stop'
$python = "$PSScriptRoot\runtime\webui-env\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Install.cmd first.' }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$arguments = @("$PSScriptRoot\integration\service_manager.py", 'start')
if ($NoBrowser) { $arguments += '--no-browser' }
if ($ManagedBrowser) { $arguments += @('--browser', $ManagedBrowser) }
& $python @arguments
exit $LASTEXITCODE
