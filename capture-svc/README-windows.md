# capture-svc on Windows (native, not Docker)

Docker Desktop on Windows (WSL2 backend) cannot pass `/dev/snd` through
to Linux containers, so capture-svc cannot reach the microphone from
inside a container on this machine. Every other service stays in
Docker; only this one runs directly on the Windows host.

## One-time setup

```
cd capture-svc
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

1. Start the other 5 services: `docker compose up -d` (from repo root)
2. `.\run-windows.ps1`

The script loads `RTSP_URL` / API keys from `..\.env`, and points
`VISION_SVC_URL` / `ORCHESTRATOR_URL` at `localhost` (the ports those
containers publish per `docker-compose.yml`) instead of the Docker
network hostnames used when everything is containerized.

## Moving to a real Linux host / Raspberry Pi later

Uncomment the `capture-svc` block in `docker-compose.yml`, remove the
`localhost` overrides here, and run it in Docker like the rest —
`/dev/snd` passthrough works normally there.
