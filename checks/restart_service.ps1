# Restart the opencode Supervisor service (port 8501) detached.
# Rotate previous logs with a timestamp suffix, then start app.py.
$ErrorActionPreference = 'Stop'
$root = 'E:\pyprojects\super-opencode'
$stamp = Get-Date -Format 'yyyyMMdd_HHmm'

foreach ($name in @('supervisor_app.out.log', 'supervisor_app.err.log')) {
    $p = Join-Path $root $name
    if (Test-Path $p) {
        Move-Item -Force -Path $p -Destination (Join-Path $root "$name.$stamp")
    }
}

Start-Process -FilePath (Join-Path $root 'venv\Scripts\python.exe') `
    -ArgumentList 'app.py' `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root 'supervisor_app.out.log') `
    -RedirectStandardError (Join-Path $root 'supervisor_app.err.log')
Write-Output "started stamp=$stamp"
