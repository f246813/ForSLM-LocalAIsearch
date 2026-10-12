param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$localPath = "$projectRoot\config\local.json"
if (-not (Test-Path -LiteralPath $localPath)) { throw 'Run Install.cmd, then edit config\local.json first.' }
$local = Get-Content -LiteralPath $localPath -Raw | ConvertFrom-Json
$env:FORSLM_NODE_PATH = (Get-Command node -ErrorAction Stop).Source
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:NO_PROXY = 'localhost,127.0.0.1,::1'
New-Item -ItemType Directory -Force -Path "$projectRoot\data", "$projectRoot\logs" | Out-Null
$processFile = "$projectRoot\runtime\processes.json"
$owned = @()
if (Test-Path -LiteralPath $processFile) {
    $owned = @(Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json)
}
function Ready([string]$url) {
    try { (Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200 }
    catch { $false }
}
function Launch([string]$name, [string]$exe, [string]$arguments, [string]$folder, [string]$health) {
    if (Ready $health) { Write-Host "$name already running"; return }
    $child = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $folder -WindowStyle Hidden -PassThru -RedirectStandardOutput "$projectRoot\logs\$name.out.log" -RedirectStandardError "$projectRoot\logs\$name.err.log"
    $script:owned += [pscustomobject]@{ name=$name; pid=$child.Id; startTime=$child.StartTime.ToUniversalTime().ToString('o'); executable=$exe }
    ConvertTo-Json -InputObject @($script:owned) | Set-Content -LiteralPath $processFile -Encoding UTF8
    $deadline = (Get-Date).AddMinutes(4)
    while ((Get-Date) -lt $deadline) {
        if (Ready $health) { Write-Host "$name ready"; return }
        $child.Refresh()
        if ($child.HasExited) { throw "$name failed. See $projectRoot\logs\$name.err.log" }
        Start-Sleep -Seconds 2
    }
    throw "$name startup timed out. See $projectRoot\logs"
}
$llamaExe = $local.llama_server
$qwenArguments = '--model "{0}" --ctx-size {1} --gpu-layers {2} --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --host 127.0.0.1 --port 8080 --jinja' -f $local.qwen_model, $local.qwen_context, $local.gpu_layers
$bgeArguments = '--model "{0}" --ctx-size {1} --gpu-layers {2} --embedding --pooling rank --reranking --host 127.0.0.1 --port 8081 --no-webui' -f $local.reranker_model, $local.reranker_context, $local.gpu_layers
Launch 'qwen' $llamaExe $qwenArguments (Split-Path $llamaExe) 'http://127.0.0.1:8080/health'
Launch 'bge' $llamaExe $bgeArguments (Split-Path $llamaExe) 'http://127.0.0.1:8081/health'
$settingsPath = "$projectRoot\config\searxng.yml"
if (-not (Test-Path -LiteralPath $settingsPath)) { Copy-Item -LiteralPath "$projectRoot\config\searxng.example.yml" -Destination $settingsPath }
$settings = Get-Content -LiteralPath $settingsPath -Raw
if ($settings.Contains('REPLACE_ON_FIRST_START')) {
    $settings.Replace('REPLACE_ON_FIRST_START', ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))) | Set-Content -LiteralPath $settingsPath -Encoding UTF8
}
$env:SEARXNG_SETTINGS_PATH = $settingsPath
$env:PYTHONPATH = "$projectRoot\upstream\searxng"
Launch 'searxng' "$projectRoot\runtime\searxng-env\Scripts\python.exe" "`"$projectRoot\integration\serve-searxng.py`"" "$projectRoot\upstream\searxng" 'http://127.0.0.1:8888/'
Remove-Item Env:PYTHONPATH
$env:USE_SLIM_DOCKER = 'true'
$env:DATA_DIR = "$projectRoot\data\open-webui"
$env:WEBUI_AUTH = 'false'
$env:WEBUI_URL = 'http://127.0.0.1:3000'
$env:ENABLE_OLLAMA_API = 'false'
$env:OPENAI_API_BASE_URL = 'http://127.0.0.1:8080/v1'
$env:OPENAI_API_KEY = 'local-llama'
$env:DEFAULT_MODELS = 'local_ai_search'
$env:ENABLE_WEB_SEARCH = 'false'
$env:ENABLE_EVALUATION_ARENA_MODELS = 'false'
$env:OFFLINE_MODE = 'true'
$env:RAG_EMBEDDING_MODEL_AUTO_UPDATE = 'false'
$env:WHISPER_MODEL_AUTO_UPDATE = 'false'
$env:ENABLE_VERSION_UPDATE_CHECK = 'false'
$env:ANONYMIZED_TELEMETRY = 'false'
$env:DO_NOT_TRACK = 'true'
$env:FORSLM_PROJECT_ROOT = $projectRoot
Launch 'open-webui' "$projectRoot\runtime\webui-env\Scripts\open-webui.exe" 'serve --host 127.0.0.1 --port 3000' $projectRoot 'http://127.0.0.1:3000/health'
& "$projectRoot\runtime\webui-env\Scripts\python.exe" "$projectRoot\integration\register-pipe.py"
if ($LASTEXITCODE -ne 0) { throw 'Could not register the search model.' }
Write-Host 'Open WebUI: http://127.0.0.1:3000'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:3000' }
