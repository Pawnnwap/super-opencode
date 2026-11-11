# One-shot stack probe + repair, fired every couple of minutes by the
# soprun_watchdog scheduled task. The repetition trigger is the loop - this
# script probes once, repairs what is broken, and exits.
#
# It used to be a long-lived watcher process started alongside the stack, so
# it shared the stack's fate: when the owning agent session was torn down
# (2026-11-08 19:18:45) the watcher died in the same sweep and the UI stayed
# down for 15+ hours with nobody left to heal it. A scheduled task instance
# has no session to be torn down with, and every repair start it performs is
# task-owned too (start_suprun.ps1 -Detached), so nothing here can be
# harvested out from under the service again.
#
# Repairs never touch a healthy worker: running jobs keep their process.
param(
  [int]$AppPort = 8501,
  [int]$ProxyPort = 8321,
  [int]$WorkerLockMaxAgeSec = 30
)
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $root 'watch_suprun.log'
$launch = Join-Path $root 'start_suprun.ps1'
$strikes = Join-Path $root '.job_store\watch_probe.state'

function Log($msg) {
  ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) |
    Out-File $log -Append -Encoding utf8
}

function Test-PortListening([int]$Port) {
  [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Test-JobWorkerAlive {
  # The daemon touches .job_store/worker.lock every poll cycle; a lock older
  # than the threshold belongs to a dead predecessor (same rule as
  # start_suprun.ps1, looser bound to ride out a busy poll cycle).
  $lock = Join-Path $root '.job_store\worker.lock'
  if (-not (Test-Path $lock)) { return $false }
  return ((Get-Date) - (Get-Item $lock).LastWriteTime).TotalSeconds -lt $WorkerLockMaxAgeSec
}

function Invoke-Repair([string]$reason, [string[]]$flags) {
  Log "repair: $reason"
  $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $launch -Detached @flags 2>&1
  foreach ($line in @($out)) { if ("$line".Trim()) { Log "  $line" } }
}

# --- app: port down is unambiguous (repair at once); port up with HTTP dead
# is ambiguous (frozen loop vs a socket blip), so it takes two consecutive
# failed probes before a restart - historical blips recovered on their own.
$appPortUp = Test-PortListening $AppPort
$appHttp = $false
if ($appPortUp) {
  try {
    # /healthz is a plain route: probing "/" would build a full UI page and
    # churn a client per probe (the old 1-per-10s "client deleted" noise).
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$AppPort/healthz" -TimeoutSec 10 -UseBasicParsing
    $appHttp = [bool]$resp.StatusCode
  } catch { $appHttp = $false }
}
$strikeCount = 0
if (Test-Path $strikes) { $strikeCount = [int](Get-Content $strikes | Select-Object -First 1) }
if ($strikeCount -lt 0) { $strikeCount = 0 }

if ($appHttp) {
  if ($strikeCount -gt 0) { Set-Content $strikes '0' }
} elseif (-not $appPortUp) {
  Invoke-Repair "app down on port $AppPort - starting the stack" @()
  Set-Content $strikes '0'
} else {
  $strikeCount += 1
  Set-Content $strikes "$strikeCount"
  if ($strikeCount -ge 2) {
    Invoke-Repair "app serving dead on port $AppPort ($strikeCount strikes) - restarting app only" @('-Restart', '-AppOnly')
    Set-Content $strikes '0'
  } else {
    Log "probe: app HTTP failed but port up (strike $strikeCount/2)"
  }
}

# --- capability proxy
$proxyUp = $false
try {
  Invoke-RestMethod -Uri "http://127.0.0.1:$ProxyPort/healthz" -TimeoutSec 10 | Out-Null
  $proxyUp = $true
} catch {}
if (-not $proxyUp) {
  Invoke-Repair "capability proxy down on port $ProxyPort" @('-ProxyOnly')
}

# --- job worker
function Get-WorkerProcesses {
  # Match the command line, never the lock's pid: a recycled pid must not get
  # an unrelated process killed.
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*-m services.jobs.worker_service*' }
}

function Stop-WorkerTree([object[]]$Procs) {
  foreach ($w in $Procs) {
    Log ("  kill: worker pid {0} (tree)" -f $w.ProcessId)
    & taskkill /F /T /PID $w.ProcessId 2>&1 | ForEach-Object { if ("$_".Trim()) { Log "  $_" } }
  }
  if ($Procs.Count -gt 0) { Start-Sleep -Seconds 3 }
}

if (-not (Test-JobWorkerAlive)) {
  $lockPath = Join-Path $root '.job_store\worker.lock'
  $lockAge = 0
  if (Test-Path $lockPath) {
    $lockAge = ((Get-Date) - (Get-Item $lockPath).LastWriteTime).TotalSeconds
  }
  $workers = @(Get-WorkerProcesses)
  # A healthy worker touches its lock every second, so 30s+ means wedged -
  # alive but unable to run its own loop (2026-11-10: a pathological regex
  # held the GIL for 11h). Such a process must be killed, not shadowed:
  # starting a second daemon would leave the first one's codex children
  # running. A worker younger than 5 minutes is probably still importing
  # (measured minutes under load), so it is left alone.
  $wedged = @($workers | Where-Object {
    ((Get-Date) - $_.CreationDate).TotalSeconds -ge 300
  })
  if ($wedged.Count -gt 0) {
    Log ("worker: lock stale {0:N0}s - killing {1} wedged process tree(s)" -f $lockAge, $wedged.Count)
    Stop-WorkerTree $wedged
  } elseif ($workers.Count -gt 0) {
    Log ("worker: start in progress (pid {0}, {1:N0}s old) - leaving it alone" -f `
      $workers[0].ProcessId, ((Get-Date) - $workers[0].CreationDate).TotalSeconds)
    exit 0
  }
  Invoke-Repair 'job worker lock stale - starting the worker' @('-AppOnly')
}

# --- queue liveness: PENDING work plus a free slot must make progress.
# Heartbeat freshness proves the process is alive, not that it is claiming.
$queueProbe = Join-Path $root 'checks\watch_queue_liveness.ps1'
$queueState = Join-Path $root '.job_store\watch_queue.state'
$queueOut = @(& powershell -NoProfile -ExecutionPolicy Bypass -File $queueProbe `
  -StoreDir (Join-Path $root '.job_store') -StateFile $queueState 2>&1)
$queueCode = $LASTEXITCODE
$queueVerdict = "$($queueOut[-1])"
if ($queueVerdict.Trim() -and $queueVerdict -notmatch '\sOK$') {
  foreach ($line in $queueOut) { if ("$line".Trim()) { Log "  $line" } }
}
if ($queueCode -eq 1 -and (Test-JobWorkerAlive)) {
  # STALLED-IDLE only: nothing is in flight, so the restart loses no work.
  # STALLED-RUNNING exits 0 (log only) and never reaches this branch.
  Log 'queue: idle worker not claiming PENDING work - restarting it'
  Stop-WorkerTree @(Get-WorkerProcesses)
  Invoke-Repair 'job worker idle but not claiming PENDING work - restarting it' @('-AppOnly')
}
