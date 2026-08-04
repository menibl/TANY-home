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
3. Open **http://127.0.0.1:8010** — live status dashboard + "משתמש חדש"
   (new user) enrollment: face photo, body photo, and a voice sample,
   sent to vision-id-svc's and voice-id-svc's `/enroll` endpoints.

The script loads `RTSP_URL` / API keys from `..\.env`, and points
`VISION_SVC_URL` / `VOICE_SVC_URL` / `ORCHESTRATOR_URL` at `localhost`
(the ports those containers publish per `docker-compose.yml`) instead
of the Docker network hostnames used when everything is containerized.

## Moving to a real Linux host / Raspberry Pi later

See [README-pi.md](README-pi.md) — capture-svc runs in Docker there
(`/dev/snd` passthrough works normally on real Linux) via its own
`docker-compose.pi.yml`, pointed at this PC's LAN IP for the other 5
services, which stay here.
