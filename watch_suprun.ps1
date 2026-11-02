# Self-healing UI watcher: HTTP-probe the app; after enough consecutive
# failures relaunch the app ONLY. The worker daemon is never touched - jobs
# keep running in their own process regardless of UI health.
param(
  [int]$AppPort = 8501,
  [int]$IntervalSec = 60,
  [int]$FailuresNeeded = 2
)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $root 'watch_suprun.log'

function Log($msg) {
  ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) |
    Out-File $log -Append -Encoding utf8
}

# One watcher is enough; a second instance exits (start_suprun re-runs would
# otherwise stack watchers).
$dup = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*watch_suprun.ps1*' }
if ($dup) {
  Log "watcher already running (pid $($dup.ProcessId)) - exiting"
  exit 0
}

Log "watcher started (port $AppPort, interval ${IntervalSec}s, relaunch after $FailuresNeeded failure(s))"
$failures = 0
while ($true) {
  Start-Sleep -Seconds $IntervalSec
  $ok = $false
  try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$AppPort/" -TimeoutSec 10 -UseBasicParsing
    $ok = [bool]$resp.StatusCode
  } catch { $ok = $false }
  if ($ok) {
    if ($failures -gt 0) { Log "probe recovered after $failure(s)" }
    $failures = 0
    continue
  }
  $failures++
  Log "probe failed ($failures/$FailuresNeeded)"
  if ($failures -ge $FailuresNeeded) {
    Log "relaunching the app (worker daemon and running jobs are untouched)"
    & (Join-Path $root 'start_suprun.ps1') -Restart -AppOnly *>> $log
    $failures = 0
  }
}
