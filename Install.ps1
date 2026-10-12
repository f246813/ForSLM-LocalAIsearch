param([string]$UVPath = '')
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_CACHE_DIR = "$projectRoot\runtime\uv-cache"
$env:UV_PYTHON_INSTALL_DIR = "$projectRoot\runtime\python"
$env:UV_PYTHON_BIN_DIR = "$projectRoot\runtime\python-bin"
$versions = Get-Content -LiteralPath "$projectRoot\config\versions.json" -Raw | ConvertFrom-Json
$gitExe = (Get-Command git -ErrorAction Stop).Source
$nodeExe = (Get-Command node -ErrorAction Stop).Source
$npmExe = (Get-Command npm.cmd -ErrorAction Stop).Source
$nodeVersion = [version]((& $nodeExe --version).Trim().TrimStart('v'))
if ($nodeVersion -lt [version]'20.18.1') { throw 'Node.js 20.18.1 or newer is required.' }
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
    throw 'This installer targets Windows x64.'
}
New-Item -ItemType Directory -Force -Path "$projectRoot\runtime", "$projectRoot\upstream", "$projectRoot\logs" | Out-Null
function Run([string]$exe, [string[]]$arguments) {
    & $exe @arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $exe $($arguments -join ' ')" }
}
function Get-Upstream([string]$name, [string]$url, [string]$commit, [switch]$SearXNG) {
    $folder = "$projectRoot\upstream\$name"
    if (-not (Test-Path -LiteralPath "$folder\.git")) {
        Run $gitExe @('clone', '--no-checkout', '--depth', '1', $url, $folder)
        Run $gitExe @('-C', $folder, 'fetch', '--depth', '1', 'origin', $commit)
        if ($SearXNG) {
            Run $gitExe @('-C', $folder, 'sparse-checkout', 'init', '--cone')
            Run $gitExe @('-C', $folder, 'sparse-checkout', 'set', 'searx')
        }
        Run $gitExe @('-C', $folder, '-c', 'core.protectNTFS=false', 'checkout', '--detach', $commit)
    } else {
        $actual = (& $gitExe -C $folder rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $actual -ne $commit) {
            throw "$name is at another revision. Keep your changes; install into a fresh project directory."
        }
    }
    return $folder
}
function Apply-Patch([string]$folder, [string]$patch) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $gitExe -C $folder apply --reverse --check $patch 2>$null
    $alreadyApplied = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $previousPreference
    if ($alreadyApplied) { Write-Host "Patch already applied: $(Split-Path $patch -Leaf)"; return }
    Run $gitExe @('-C', $folder, 'apply', $patch)
}
if (-not $UVPath) {
    $existing = "$projectRoot\runtime\uv-package\bin\uv.exe"
    $downloaded = "$projectRoot\runtime\uv\uv.exe"
    if (Test-Path -LiteralPath $existing) { $UVPath = $existing }
    elseif (Test-Path -LiteralPath $downloaded) { $UVPath = $downloaded }
    else {
        $zipPath = "$projectRoot\runtime\uv-download.zip"
        $uvURL = "https://github.com/astral-sh/uv/releases/download/$($versions.uv)/uv-x86_64-pc-windows-msvc.zip"
        Write-Host "Downloading uv $($versions.uv)..."
        Invoke-WebRequest -Uri $uvURL -OutFile $zipPath -UseBasicParsing
        Expand-Archive -LiteralPath $zipPath -DestinationPath "$projectRoot\runtime\uv" -Force
        $UVPath = $downloaded
    }
}
Run $UVPath @('python', 'install', $versions.python)
$pythonExe = "$projectRoot\runtime\python\cpython-$($versions.python)-windows-x86_64-none\python.exe"
foreach ($name in 'searxng-env', 'webui-env') {
    $envFolder = "$projectRoot\runtime\$name"
    if (-not (Test-Path -LiteralPath "$envFolder\Scripts\python.exe")) {
        Run $UVPath @('venv', '--python', $pythonExe, $envFolder)
    }
}
$agentFolder = Get-Upstream 'agent-search' 'https://github.com/sebrinass/agent-search.git' $versions.agent_search_source
$searxFolder = Get-Upstream 'searxng' 'https://github.com/searxng/searxng.git' $versions.searxng_source -SearXNG
Apply-Patch $agentFolder "$projectRoot\integration\agent-search.patch"
Apply-Patch $searxFolder "$projectRoot\integration\searxng-windows.patch"
Push-Location $agentFolder
try {
    Run $npmExe @('ci', '--no-audit', '--no-fund')
    Run $npmExe @('run', 'build')
} finally { Pop-Location }
# Exact package versions from the working local deployment. Open WebUI is installed
# without dependency resolution so the official slim set doesn't pull local AI stacks.
Run $UVPath @('pip', 'install', '--no-deps', '--python', "$projectRoot\runtime\webui-env\Scripts\python.exe", '-r', "$projectRoot\config\webui-requirements.lock.txt")
Run $UVPath @('pip', 'install', '--no-deps', '--python', "$projectRoot\runtime\searxng-env\Scripts\python.exe", '-r', "$projectRoot\config\searxng-requirements.lock.txt")
foreach ($name in 'local.json', 'searxng.yml') {
    $path = "$projectRoot\config\$name"
    if (-not (Test-Path -LiteralPath $path)) {
        $example = if ($name -eq 'local.json') { 'local.example.json' } else { 'searxng.example.yml' }
        Copy-Item -LiteralPath "$projectRoot\config\$example" -Destination $path
    }
}
Write-Host 'Installation complete. Edit config\local.json to point to llama-server and both GGUF models, then run Start.cmd.'
