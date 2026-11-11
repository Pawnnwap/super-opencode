# Claim-liveness probe for the job queue.
#
# Heartbeat freshness proves the worker PROCESS is alive; it cannot see an
# alive-but-not-claiming worker (engine missing, a stuck deployment hold, a
# claim bug) while PENDING work waits with free run slots. This probe watches
# that condition across watchdog cycles and reports it:
#
#   OK                   no PENDING work, or every slot is busy
#   WARN                 stall, younger than -StallMinutes
#   STALLED-RUNNING      stall with jobs in flight - log only (never kill)
#   STALLED-IDLE         stall with nothing running - safe to restart the
#                        worker (exit 1; the caller repairs)
#   STALLED-IDLE-BACKOFF same, but a repair was already tried recently
#
# The clock resets whenever a count changes or the worker pid changes, so a
# healthy queue draining one job at a time never looks stalled.

param(
  [string]$StoreDir = '',
  [string]$StateFile = '',
  [int]$StallMinutes = 5,
  [int]$RepairBackoffMinutes = 30,
  [int]$MaxParallel = 0
)
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $StoreDir) { $StoreDir = Join-Path $root '.job_store' }
if (-not $StateFile) { $StateFile = Join-Path $StoreDir 'watch_queue.state' }

$max = $MaxParallel
if ($max -le 0) {
  $settings = Join-Path $StoreDir 'worker_settings.json'
  if (Test-Path $settings) {
    try { $max = [int]((Get-Content $settings -Raw | ConvertFrom-Json).max_parallel_jobs) } catch {}
  }
}
if ($max -le 0) { $max = 4 }

$pending = 0
$running = 0
foreach ($f in Get-ChildItem $StoreDir -Filter '*.json' -ErrorAction SilentlyContinue) {
  try { $s = Get-Content $f.FullName -Raw | ConvertFrom-Json } catch { continue }
  # worker_settings.json has no state field; only job records count.
  if ($s.state -eq 'PENDING') { $pending++ }
  elseif ($s.state -eq 'RUNNING') { $running++ }
}

$workerPid = $null
$lock = Join-Path $StoreDir 'worker.lock'
if (Test-Path $lock) {
  try { $workerPid = (Get-Content $lock -Raw | ConvertFrom-Json).pid } catch {}
}

$now = [DateTimeOffset]::Now.ToUnixTimeSeconds()
$since = $now
$lastPid = $null
$lastPending = -1
$lastRunning = -1
$lastRepair = 0
if (Test-Path $StateFile) {
  try {
    $st = Get-Content $StateFile -Raw | ConvertFrom-Json
    if ($null -ne $st.since) { $since = [long]$st.since }
    if ($null -ne $st.pending) { $lastPending = [int]$st.pending }
    if ($null -ne $st.running) { $lastRunning = [int]$st.running }
    if ($null -ne $st.last_repair) { $lastRepair = [long]$st.last_repair }
    $lastPid = $st.pid
  } catch {}
}

# Any change in the queue or in the worker process is progress: restart the clock.
if ($workerPid -ne $lastPid -or $pending -ne $lastPending -or $running -ne $lastRunning) {
  $since = $now
}

$stalledFor = $now - $since
$line = "queue: pending=$pending running=$running max=$max stalled=${stalledFor}s"
$verdict = 'OK'
$code = 0
if ($pending -gt 0 -and $running -lt $max) {
  if ($stalledFor -lt ($StallMinutes * 60)) {
    $verdict = 'WARN'
  } elseif ($running -gt 0) {
    # Running jobs must not be killed to fix a claim stall.
    $verdict = 'STALLED-RUNNING'
  } elseif ($lastRepair -gt 0 -and ($now - $lastRepair) -lt ($RepairBackoffMinutes * 60)) {
    $verdict = 'STALLED-IDLE-BACKOFF'
  } else {
    $verdict = 'STALLED-IDLE'
    $lastRepair = $now
    $code = 1
  }
}

$payload = @{
  since = $since; pid = $workerPid; pending = $pending; running = $running
  last_repair = $lastRepair
} | ConvertTo-Json -Compress
[IO.File]::WriteAllText($StateFile, $payload, (New-Object System.Text.UTF8Encoding($false)))

Write-Output "$line $verdict"
exit $code
