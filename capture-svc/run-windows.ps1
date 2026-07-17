# Runs capture-svc natively on Windows (not in Docker) because Docker
# Desktop on Windows cannot pass /dev/snd (microphone) through to Linux
# containers. The other 4 services (vision-id-svc, voice-id-svc,
# orchestrator, tany-bridge) run in Docker and are reached over
# localhost via the ports published in docker-compose.yml.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $here "..\.env"

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)=(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            if ($name -and $value) {
                [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
            }
        }
    }
} else {
    Write-Warning ".env not found at $envFile - RTSP_URL must be set some other way"
}

# 127.0.0.1, not localhost: the websockets client hangs/times out
# resolving "localhost" to IPv6 first on this Windows/asyncio setup
$env:VISION_SVC_URL = "http://127.0.0.1:8001"
$env:VOICE_SVC_URL = "http://127.0.0.1:8002"
$env:ORCHESTRATOR_URL = "http://127.0.0.1:8004"

& "$here\venv\Scripts\python.exe" "$here\main.py"
