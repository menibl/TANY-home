FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    libavfilter-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8004
# /certs is a bind mount (see docker-compose.yml) shared with capture-svc's
# `tailscale cert` output — serve HTTPS under the same Tailscale hostname
# when present (needed so a browser loading the https:// dashboard can
# call this API at all: mixed-content rules block http:// calls from an
# https:// page), otherwise plain HTTP for LAN/localhost use.
CMD ["sh", "-c", "if [ -f /certs/tailscale.crt ] && [ -f /certs/tailscale.key ]; then exec uvicorn main:app --host 0.0.0.0 --port 8004 --ssl-certfile /certs/tailscale.crt --ssl-keyfile /certs/tailscale.key; else exec uvicorn main:app --host 0.0.0.0 --port 8004; fi"]
