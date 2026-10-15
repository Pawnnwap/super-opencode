# One real codex turn through codex-suprun.cmd + the capability proxy.
# Usage: .\smoke_suprun.ps1 [-Port 8322] [-Workspace <dir>]
param(
  [int]$Port = 8322,
  [string]$Workspace = 'E:\pyprojects\llm_deep_research\sop_indicator_study_1015_1',
  [string]$Upstream = $env:SOP_UPSTREAM
)
$ErrorActionPreference = 'SilentlyContinue'
$root = 'E:\pyprojects\super-opencode'
Set-Location $Workspace
Remove-Item 'mcp_smoke.txt'

if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
  if (-not $Upstream) {
    Write-Output "no upstream: pass -Upstream http://HOST:PORT or set SOP_UPSTREAM"
    exit 2
  }
  Start-Process -FilePath "$root\venv\Scripts\python.exe" `
    -ArgumentList '-u', 'codex_compat_proxy.py', "$Port", $Upstream `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput "$root\proxy$Port.out.log" -RedirectStandardError "$root\proxy$Port.err.log" | Out-Null
  Start-Sleep -Seconds 2
}

$env:SUPER_OPENCODE_CODEX_KEY = 'longwin'
$cx = "$root\codex-suprun.cmd"
$flags = @('exec', '--json', '--yolo', '--skip-git-repo-check', '--color', 'never',
  '-c', 'features.multi_agent=false',
  '-c', "model_providers.super_opencode.base_url=http://127.0.0.1:$Port/v1",
  '--', '-')
$prompt = 'Do exactly three things, in order. (1) Call the web_search MCP tool with query "vwap anomaly" and note how many results it returned. (2) Use apply_patch to create mcp_smoke.txt containing the word hello. (3) Reply with the single word OK.'
$out = $prompt | & $cx @flags 2>&1
"exit=$LASTEXITCODE"
$out | Select-String -Pattern '"type":"error"|mcp__|apply_patch|"type":"error"' | Select-Object -First 6 | Out-String -Width 170
Write-Output '--- files ---'
if (Test-Path 'mcp_smoke.txt') { Get-Content 'mcp_smoke.txt' } else { Write-Output 'NO FILE' }
Write-Output '--- proxy log (this port) ---'
Get-Content "$root\codex_compat_proxy.log.$Port" -Tail 5 | Out-String -Width 200
Write-Output '--- profile ---'
Get-Content "$root\codex_capability_cache.json.$Port" -Raw | Out-String -Width 200
