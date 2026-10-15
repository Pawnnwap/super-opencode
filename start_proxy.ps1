# Start the capability proxy (production port 8321 unless -Port given) and print /healthz.
# Upstream = -Upstream, else $env:SOP_UPSTREAM (scheme+host+port, no /v1).
param(
  [int]$Port = 8321,
  [string]$Upstream = $env:SOP_UPSTREAM
)
$root = 'E:\pyprojects\super-opencode'
$running = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $running) {
  if (-not $Upstream) {
    Write-Output "no upstream: pass -Upstream http://HOST:PORT or set SOP_UPSTREAM"
    exit 2
  }
  $p = Start-Process -FilePath "$root\venv\Scripts\python.exe" `
    -ArgumentList '-u', 'codex_compat_proxy.py', "$Port", $Upstream `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput "$root\codex_compat_proxy.out.log" -RedirectStandardError "$root\codex_compat_proxy.err.log" -PassThru
  Write-Output "started proxy pid=$($p.Id) on $Port"
  Start-Sleep -Seconds 3
} else {
  Write-Output "proxy already listening on $Port (pid $($running.OwningProcess -join ','))"
}
try {
  $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 15
  Write-Output ("healthz upstream=" + $h.upstream + " requests=" + $h.stats.requests + " rewrites=" + $h.stats.rewrites + " learned=" + $h.stats.learned + " errors=" + $h.stats.errors)
  foreach ($k in $h.profiles.PSObject.Properties.Name) {
    $v = $h.profiles.$k
    Write-Output ("profile " + $k + " grammar=" + $v.grammar_custom_tools + " namespace=" + $v.namespace_tools + " web_search=" + $v.web_search_tool + " efforts=" + ($v.responses_efforts -join '/') + " chat=" + ($v.chat_efforts -join '/'))
  }
} catch {
  Write-Output "healthz failed: $_"
}
