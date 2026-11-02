# One-click start of the super-opencode service stack:
#   capability proxy (127.0.0.1:<ProxyPort> -> -Upstream) + NiceGUI app (127.0.0.1:<AppPort>).
#
# Idempotent: pieces already listening are left running. The proxy is the one
# exception - when its live upstream differs from -Upstream it is restarted,
# so the parameter always wins. -Restart relaunches the app even when the
# port is up; -ProxyOnly / -AppOnly scope the run to a single piece.
#
# Examples:
#   .\start_suprun.ps1                                  # defaults: upstream http://127.0.0.1:8111
#   .\start_suprun.ps1 -Upstream http://192.168.18.65   # different model endpoint
#   .\start_suprun.ps1 -Restart                         # relaunch the app only
#   .\start_suprun.ps1 -ProxyOnly -Upstream http://127.0.0.1:8111
param(
  [string]$Upstream = 'http://127.0.0.1:8111',
  [int]$ProxyPort = 8321,
  [int]$AppPort = 8501,
  [switch]$Restart,
  [switch]$ProxyOnly,
  [switch]$AppOnly,
  [int]$ReadyTimeoutSec = 600
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root 'venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyyMMdd_HHmm'
$upstream = $Upstream.TrimEnd('/')

function Test-PortListening([int]$Port) {
  [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Get-PortOwnerPid([int]$Port) {
  (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -First 1).OwningProcess
}

function Stop-ListenerTree([int]$OwnerPid) {
  # Tree-kill the listener's launcher chain (venv python shim -> real
  # python, or cmd.exe wrapper -> python). Killing only the listening
  # process leaves ancestors holding file handles and orphaned stacks.
  if (-not $OwnerPid) { return }
  $topPid = $OwnerPid
  $cur = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnerPid" -ErrorAction SilentlyContinue
  while ($cur -and $cur.Name -in @('python.exe', 'cmd.exe')) {
    $topPid = $cur.ProcessId
    $cur = Get-CimInstance Win32_Process -Filter "ProcessId=$($cur.ParentProcessId)" -ErrorAction SilentlyContinue
  }
  taskkill /PID $topPid /T /F | Out-Null
}

function Stop-ServiceProcess([int]$Port) {
  # Tree-kill the port owner's launcher chain (cmd.exe wrapper -> python).
  # Killing only the listening python leaves the cmd parent holding the
  # rotated log file open, which breaks rotation and orphans the stack.
  $ownerPid = Get-PortOwnerPid $Port
  if (-not $ownerPid) { return }
  Write-Output "app   : stopping service tree (top pid lookup for listener pid=$ownerPid)"
  Stop-ListenerTree $ownerPid
  Start-Sleep -Seconds 3
}

function Start-CapabilityProxy {
  if (Test-PortListening $ProxyPort) {
    $live = ''
    try {
      $live = [string](Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/healthz" -TimeoutSec 10).upstream
      $live = $live.TrimEnd('/')
    } catch {}
    if ($live -eq $upstream) {
      Write-Output "proxy : already running on $ProxyPort -> $live (skipped)"
      return
    }
    $ownerPid = Get-PortOwnerPid $ProxyPort
    Write-Output "proxy : upstream mismatch (running='$live' wanted='$upstream') - restarting listener pid=$ownerPid"
    Stop-ListenerTree $ownerPid
    Start-Sleep -Seconds 2
  }
  # Redirect INSIDE cmd (see Start-SupervisorApp) and make sure the inner
  # command does not START with a quoted token: PowerShell wraps $inner in
  # one more quote pair, and cmd /c strips the outer quotes - an inner
  # command starting with a quoted path then loses its opening quote and
  # cmd dies on the mangled line before any redirection happens.
  $inner = 'cd /d "{0}" && "{1}" -u codex_compat_proxy.py {2} {3} > "{4}" 2> "{5}"' -f `
    $root, $python, $ProxyPort, $upstream,
    (Join-Path $root 'codex_compat_proxy.out.log'), (Join-Path $root 'codex_compat_proxy.err.log')
  Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/c', $inner) `
    -WorkingDirectory $root -WindowStyle Hidden
  $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (Test-PortListening $ProxyPort) {
      Write-Output "proxy : READY port=$ProxyPort upstream=$upstream"
      return
    }
  }
  Write-Output 'proxy : NOT READY after 30s - inspect codex_compat_proxy.err.log'
  exit 1
}

function Start-SupervisorApp {
  if (Test-PortListening $AppPort) {
    if (-not $Restart) {
      Write-Output "app   : already running on $AppPort (use -Restart to relaunch)"
      return
    }
    Stop-ServiceProcess $AppPort
  }
  foreach ($name in @('supervisor_app.out.log', 'supervisor_app.err.log')) {
    $p = Join-Path $root $name
    if (Test-Path $p) {
      # Rotate; a dying process may still hold the file open - fall back to
      # removing it rather than failing the whole launch.
      try {
        Move-Item -Force -Path $p -Destination (Join-Path $root "$name.$stamp")
      } catch {
        Remove-Item -Force -Path $p -ErrorAction SilentlyContinue
      }
    }
  }
  # Redirect INSIDE cmd (never via Start-Process -Redirect*): when the
  # launcher itself runs with its stdout attached to a pipe, Start-Process
  # children can inherit that pipe write-end and whoever captures our
  # output (a pipeline) then hangs waiting for EOF forever.
  $inner = 'set PYTHONDONTWRITEBYTECODE=1&& cd /d "{0}" && "{1}" -u app.py > "{2}" 2> "{3}"' -f `
    $root, $python, (Join-Path $root 'supervisor_app.out.log'), (Join-Path $root 'supervisor_app.err.log')
  Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/c', $inner) `
    -WorkingDirectory $root `
    -WindowStyle Hidden
  # "Started" is not success: the import graph (openai + NiceGUI) can take
  # minutes under load, so prove the port before declaring READY.
  $deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    if (Test-PortListening $AppPort) {
      Write-Output "app   : READY http://127.0.0.1:$AppPort (waited $([int]($ReadyTimeoutSec - ($deadline - (Get-Date)).TotalSeconds))s)"
      return
    }
  }
  Write-Output "app   : NOT READY after ${ReadyTimeoutSec}s - inspect supervisor_app.err.log"
  exit 1
}

function Test-JobWorkerAlive {
  # The daemon touches .job_store/worker.lock every poll cycle; a lock older
  # than ~15s belongs to a dead predecessor.
  $lock = Join-Path $root '.job_store\worker.lock'
  if (-not (Test-Path $lock)) { return $false }
  return ((Get-Date) - (Get-Item $lock).LastWriteTime).TotalSeconds -lt 15
}

function Start-JobWorker {
  # Execute jobs in a dedicated process (SOP_JOB_WORKER=1) so codex
  # subprocess chains and scanner storms never run in the serving process.
  # Singleton via the daemon's own lock; -Restart leaves it untouched, so
  # running jobs survive UI restarts.
  if (Test-JobWorkerAlive) {
    Write-Output 'worker: daemon already running (lock fresh) - skipped'
    return
  }
  foreach ($name in @('job_worker.out.log', 'job_worker.err.log')) {
    $p = Join-Path $root $name
    if (Test-Path $p) {
      try {
        Move-Item -Force -Path $p -Destination (Join-Path $root "$name.$stamp")
      } catch {
        Remove-Item -Force -Path $p -ErrorAction SilentlyContinue
      }
    }
  }
  $inner = 'set SOP_JOB_WORKER=1&& cd /d "{0}" && "{1}" -u -m services.jobs.worker_service > "{2}" 2> "{3}"' -f `
    $root, $python, (Join-Path $root 'job_worker.out.log'), (Join-Path $root 'job_worker.err.log')
  Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/c', $inner) `
    -WorkingDirectory $root -WindowStyle Hidden
  $deadline = (Get-Date).AddSeconds(15)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
    if (Test-JobWorkerAlive) {
      Write-Output 'worker: READY (lock acquired)'
      return
    }
  }
  Write-Output 'worker: NOT READY after 15s - inspect job_worker.err.log'
  exit 1
}

function Start-UiWatcher {
  # Self-heal: relaunch the app (never the worker) when serving dies.
  $inner = 'powershell -NoProfile -ExecutionPolicy Bypass -File "{0}\watch_suprun.ps1" -AppPort {1}' -f $root, $AppPort
  Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/c', $inner) `
    -WorkingDirectory $root -WindowStyle Hidden
  Write-Output "watcher: UI self-heal probe armed (port $AppPort, relaunches app only)"
}

if ($ProxyOnly -and $AppOnly) {
  Write-Output 'pick at most one of -ProxyOnly / -AppOnly'
  exit 2
}
if (-not $AppOnly) { Start-CapabilityProxy }
if (-not $ProxyOnly) {
  Start-JobWorker
  Start-SupervisorApp
}
Write-Output "service: http://127.0.0.1:$AppPort  (proxy $ProxyPort -> $upstream)"
