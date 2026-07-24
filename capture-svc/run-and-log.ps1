$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONUNBUFFERED = "1"
& "$here\run-windows.ps1" 2>&1 | Tee-Object -FilePath (Join-Path $here "capture-svc.log")
