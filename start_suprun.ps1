# One-click start of the super-opencode service stack:
#   capability proxy (127.0.0.1:<ProxyPort> -> -Upstream) + NiceGUI app (127.0.0.1:<AppPort>).
#
# OWNERSHIP RULE - the reason this script looks the way it does: processes
# started from an agent/shell session are harvested when that session ends
# (2026-11-08 19:18:45: app, proxy, worker AND the self-heal watcher all died
# in one second because every one of them was a session descendant). Only the
# Task Scheduler may own the service processes. So a session invocation of
# this script never starts anything itself - it records what was asked for
# and triggers the task-owned action (soprun_detached_launch), then waits
# for readiness. The task action runs this script with -Detached, and only
# that path spawns processes.
#
# Idempotent: pieces already listening are left running. The proxy is the one
# exception - when its live upstream differs from -Upstream it is restarted,
# so the parameter always wins. -Restart relaunches the app even when the
# port is up (the kill may run anywhere; only the START must be task-owned).
# -ProxyOnly / -AppOnly scope the run to a single piece (in-task calls).
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
  [int]$ReadyTimeoutSec = 600,
  [switch]$Detached
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root 'venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyyMMdd_HHmm'
$upstream = $Upstream.TrimEnd('/')
if (-not $PSBoundParameters.ContainsKey('Upstream')) {
  # Task-owned runs have no command line to carry -Upstream; the delegated
  # caller records its request here (plain name: the job store globs *.json).
  $pending = Join-Path $root '.job_store\launch.pending'
  if (Test-Path $pending) {
    try {
      $asked = (Get-Content $pending -Raw | ConvertFrom-Json).upstream
      if ($asked) { $upstream = $asked.TrimEnd('/') }
    } catch {}
  }
}

function Test-PortListening([int]$Port) {
  [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Write-PortsFile {
  # The guardian reads this instead of hard-coding 8501/8321, so a custom
  # -AppPort / -ProxyPort stays fully configurable end to end.
  $store = Join-Path $root '.job_store'
  if (-not (Test-Path $store)) { New-Item -ItemType Directory -Path $store | Out-Null }
  @{ app = $AppPort; proxy = $ProxyPort; upstream = $upstream } | ConvertTo-Json |
    Set-Content (Join-Path $store 'ports.json') -Encoding utf8
}

function Sync-CodexHomeProxyPort {
  # The dedicated codex home names the proxy base_url; keep it in step with
  # -ProxyPort (written BOM-free so the TOML parser keeps working).
  $cfg = Join-Path $root '.codex_home_soprun\config.toml'
  if (-not (Test-Path $cfg)) { return }
  $text = Get-Content $cfg -Raw
  $updated = [regex]::Replace(
    $text,
    'base_url\s*=\s*"http://127\.0\.0\.1:\d+/v1"',
    ('base_url = "http://127.0.0.1:{0}/v1"' -f $ProxyPort)
  )
  if ($updated -ne $text) {
    [IO.File]::WriteAllText($cfg, $updated, (New-Object System.Text.UTF8Encoding($false)))
  }
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
  Sync-CodexHomeProxyPort
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
  # The trailing exit-code sentinel is the death recorder: a clean exit
  # appends its code here, a hard kill (taskkill / job-object teardown)
  # leaves the file absent - the one distinction stderr can no longer make.
  # Delayed expansion (!VAR!) on purpose: cmd expands %VAR% once at parse
  # time, which would record the LAUNCH-time errorlevel/date instead of the
  # exit-time one. No launched path may contain "!" (none do).
  $inner = 'cd /d "{0}" && "{1}" -u codex_compat_proxy.py {2} {3} > "{4}" 2> "{5}" & echo !DATE! !TIME! exitcode=!ERRORLEVEL!>> "{6}"' -f `
    $root, $python, $ProxyPort, $upstream,
    (Join-Path $root 'codex_compat_proxy.out.log'), (Join-Path $root 'codex_compat_proxy.err.log'),
    (Join-Path $root 'codex_compat_proxy.exitcode')
  Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/v:on', '/c', $inner) `
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
  # Exit-code sentinel: appended only when python exits on its own (see
  # Start-CapabilityProxy) - its absence marks a hard kill. Delayed
  # expansion so the recorded code/time are the EXIT ones, not launch-time.
  $inner = 'set PYTHONDONTWRITEBYTECODE=1&& cd /d "{0}" && "{1}" -u app.py > "{2}" 2> "{3}" & echo !DATE! !TIME! exitcode=!ERRORLEVEL!>> "{4}"' -f `
    $root, $python, (Join-Path $root 'supervisor_app.out.log'), (Join-Path $root 'supervisor_app.err.log'),
    (Join-Path $root 'supervisor_app.exitcode')
  Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/v:on', '/c', $inner) `
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
  $outLog = Join-Path $root 'job_worker.out.log'
  $errLog = Join-Path $root 'job_worker.err.log'
  $rotated = $true
  foreach ($p in @($outLog, $errLog)) {
    if (Test-Path $p) {
      try {
        Move-Item -Force -Path $p -Destination "$p.$stamp"
      } catch {
        $rotated = $false
      }
    }
  }
  if (-not $rotated) {
    # A wedged predecessor can still hold the canonical logs open. cmd silently
    # skips the python command when its redirect cannot open the file (the
    # "worker" then exits immediately and every repair repeats), so use
    # per-start names instead of failing the whole start.
    $outLog = "$outLog.$stamp"
    $errLog = "$errLog.$stamp"
  }
  # Exit-code sentinel: appended only when python exits on its own (see
  # Start-CapabilityProxy) - its absence marks a hard kill. Delayed
  # expansion so the recorded code/time are the EXIT ones, not launch-time.
  $inner = 'set SOP_JOB_WORKER=1&& cd /d "{0}" && "{1}" -u -m services.jobs.worker_service > "{2}" 2> "{3}" & echo !DATE! !TIME! exitcode=!ERRORLEVEL!>> "{4}"' -f `
    $root, $python, $outLog, $errLog,
    (Join-Path $root 'job_worker.exitcode')
  Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/v:on', '/c', $inner) `
    -WorkingDirectory $root -WindowStyle Hidden
  $deadline = (Get-Date).AddSeconds(15)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
    if (Test-JobWorkerAlive) {
      Write-Output 'worker: READY (lock acquired)'
      return
    }
  }
  Write-Output 'worker: still starting after 15s (imports can take minutes) - process left running'
}

function Ensure-ScheduledTasks {
  # Ownership rule (2026-11-08 19:18:45): a session-owned healer dies in the
  # same harvest that kills the stack, so the start handle and the healer are
  # Task Scheduler tasks:
  #   soprun_detached_launch   - on-demand start handle (schtasks /Run)
  #   soprun_guardian          - ONE long-lived pythonw process (no console)
  #                              that probes and repairs the stack
  #   soprun_guardian_backstop - every 15 min, pythonw --once: restarts the
  #                              guardian itself when its heartbeat goes stale
  # The guardian is plain Python (services/runtime/guardian.py), so the
  # monitoring logic is portable; only its launcher command is Windows-bound.
  $launchSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  $guardianSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
  $pythonw = Join-Path $root 'venv\Scripts\pythonw.exe'
  $guardianPy = Join-Path $root 'services\runtime\guardian.py'

  $launchAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}\start_suprun.ps1" -Detached' -f $root) `
    -WorkingDirectory $root
  $launchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(5)
  Register-ScheduledTask -TaskName soprun_detached_launch -Action $launchAction `
    -Trigger $launchTrigger -Settings $launchSettings -Force | Out-Null

  # pythonw owns no console at all - the 2-minute PowerShell pop-up is gone.
  $guardianAction = New-ScheduledTaskAction -Execute $pythonw `
    -Argument ('"{0}"' -f $guardianPy) -WorkingDirectory $root
  # -AtLogOn without -User means "any user" and needs elevation; the service
  # belongs to the operator's session, so bind the trigger to that user.
  $guardianTrigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
  Register-ScheduledTask -TaskName soprun_guardian -Action $guardianAction `
    -Trigger $guardianTrigger -Settings $guardianSettings -Force | Out-Null

  $backstopAction = New-ScheduledTaskAction -Execute $pythonw `
    -Argument ('"{0}" --once' -f $guardianPy) -WorkingDirectory $root
  # Sparse first fire + 15-min repetition (MaxValue duration is rejected).
  $backstopTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
  Register-ScheduledTask -TaskName soprun_guardian_backstop -Action $backstopAction `
    -Trigger $backstopTrigger -Settings $guardianSettings -Force | Out-Null

  # Retired 2-minute poller: remove the task (its script is deleted too).
  Unregister-ScheduledTask -TaskName soprun_watchdog -Confirm:$false -ErrorAction SilentlyContinue
  schtasks /Run /TN soprun_guardian 2>&1 | Out-Null
  Write-Output 'tasks  : soprun_detached_launch + soprun_guardian (pythonw) + soprun_guardian_backstop (15min) registered'
}

function Invoke-DelegatedStart {
  # Runs in a session (agent tool call, human console). Spawning the service
  # from here would make the session its owner and the next session end its
  # grave - so this only records the request and triggers the task-owned
  # action. The kill side of -Restart is fine from anywhere: only process
  # OWNERSHIP must stay with the Task Scheduler.
  if ($Restart -and (Test-PortListening $AppPort)) {
    Stop-ServiceProcess $AppPort
  }
  Write-PortsFile
  @{ upstream = $upstream } | ConvertTo-Json |
    Set-Content (Join-Path $root '.job_store\launch.pending') -Encoding utf8
  Ensure-ScheduledTasks
  schtasks /Run /TN soprun_detached_launch 2>&1 | Out-Null  # an already-running instance is fine
  Write-Output 'start  : delegated to scheduled task soprun_detached_launch (session-immune ownership)'
  $deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    if ((Test-PortListening $AppPort) -and (Test-PortListening $ProxyPort) -and (Test-JobWorkerAlive)) {
      Write-Output "service: http://127.0.0.1:$AppPort  (proxy $ProxyPort -> $upstream)"
      return
    }
  }
  Write-Output "service: NOT READY after ${ReadyTimeoutSec}s - inspect supervisor_app.err.log / job_worker.err.log"
  exit 1
}

if ($ProxyOnly -and $AppOnly) {
  Write-Output 'pick at most one of -ProxyOnly / -AppOnly'
  exit 2
}
if (-not $Detached) {
  Invoke-DelegatedStart
  exit 0
}
if (-not $AppOnly) { Start-CapabilityProxy }
if (-not $ProxyOnly) {
  Write-PortsFile
  Start-JobWorker
  Start-SupervisorApp
}
Write-Output "service: http://127.0.0.1:$AppPort  (proxy $ProxyPort -> $upstream)"
