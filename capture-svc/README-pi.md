# capture-svc on Raspberry Pi (headless, BT speaker+mic)

Scope of this move: **only capture-svc** runs on the Pi. Every other
service (orchestrator, vision-id-svc, voice-id-svc, tany-bridge,
profile-store) stays on the PC, exactly as it runs today — the Pi just
becomes the physical presence (mic/speaker/camera-trigger/button) and
talks to the PC over the LAN. Face identification still uses the RTSP
camera already configured in `.env`, not anything attached to the Pi.

Replace `<PC_LAN_IP>` below with this PC's LAN IP: **192.168.68.58**
(confirm with `ipconfig` on the PC if it's changed since).

## Recommended: no browser on the Pi at all

The Pi is headless, so the WebRTC/browser conversation path
(`USE_REALTIME=1`, what the Windows dashboard uses) doesn't fit — there's
no browser anywhere to hold the mic. Use the **server-side OpenAI
Realtime relay** instead (`orchestrator/realtime_relay.py`, added
recently): capture-svc's own mic/speaker talk directly through the
orchestrator to OpenAI, no browser needed, still low-latency.

On the **PC**, add to `.env` and restart orchestrator:
```
LLM_PROVIDER=openai
```
```powershell
docker compose up -d --build orchestrator
```
This only affects sessions that go through the old `/session` websocket
(what the Pi will use, since it runs with `USE_REALTIME=0`) — it does
**not** touch the Windows dashboard's WebRTC path, which never uses
`/session` at all. Both machines can keep working simultaneously.

---

## 1. Flash the OS (headless — no monitor needed)

1. Install **Raspberry Pi Imager** on this PC.
2. Choose **Raspberry Pi OS Lite (64-bit)** — no desktop needed.
3. Before writing, click the gear icon (advanced options) and set:
   - Enable SSH (use password or your public key)
   - Set username/password
   - Configure your WiFi SSID/password (or skip if using Ethernet)
   - Set hostname, e.g. `homebot-pi`
4. Write to the SD card, boot the Pi, wait ~1 min, then from this PC:
   ```bash
   ssh <username>@homebot-pi.local
   ```

## 2. Update + install Docker

```bash
sudo apt update && sudo apt full-upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
sudo reboot
```
SSH back in after reboot, then confirm:
```bash
docker run hello-world
```

## 3. Pair the Bluetooth speaker/mic

```bash
sudo apt install -y bluez-alsa-utils
bluetoothctl
```
Inside `bluetoothctl`:
```
power on
agent on
scan on
# wait for your device to appear, note its MAC address (XX:XX:XX:XX:XX:XX)
pair XX:XX:XX:XX:XX:XX
trust XX:XX:XX:XX:XX:XX
connect XX:XX:XX:XX:XX:XX
exit
```
Verify it shows up as an ALSA device:
```bash
aplay -l    # should list the BT speaker
arecord -l  # should list the BT mic — if empty, your device is
            # output-only (A2DP) and doesn't support a mic (HSP/HFP);
            # you'd need a separate USB mic in that case
```
Make it the **default** ALSA device (so `device=None` in capture-svc's
code picks it up automatically):
```bash
cat > ~/.asoundrc <<'EOF'
pcm.!default {
    type asym
    playback.pcm "bluealsa"
    capture.pcm "bluealsa"
}
pcm.bluealsa {
    type bluealsa
    device "XX:XX:XX:XX:XX:XX"
    profile "a2dp"
}
EOF
```
Replace `XX:XX:XX:XX:XX:XX` with the real MAC address. Test:
```bash
speaker-test -t wav -c 1
arecord -d 3 test.wav && aplay test.wav
```

## 4. Open the required ports on the PC's firewall

The Pi needs to reach vision-id-svc (8001) and voice-id-svc (8002) on
the PC directly, in addition to orchestrator (8004) already opened
earlier. On the **PC** (elevated PowerShell):
```powershell
New-NetFirewallRule -DisplayName "HomeBot Pi" -Direction Inbound -LocalPort 8001,8002 -Protocol TCP -Action Allow
```

## 5. Clone the repo on the Pi

```bash
git clone git@github.com:menibl/TANY-home.git
cd TANY-home/homebot
```
(If SSH keys aren't set up on the Pi yet, clone via HTTPS with a
personal access token instead.)

## 6. Configure the Pi's `.env`

Create `homebot/.env` on the Pi (same directory as `docker-compose.pi.yml`'s
parent) — only capture-svc's own tunables plus the PC's addresses, no
API keys needed here (those live on the PC, which does the actual LLM
calls):
```
RTSP_URL=rtsp://admin:12345@192.168.68.80/live/main
VISION_SVC_URL=http://<PC_LAN_IP>:8001
VOICE_SVC_URL=http://<PC_LAN_IP>:8002
ORCHESTRATOR_URL=http://<PC_LAN_IP>:8004
CLAP_ENERGY_THRESHOLD=0.08
CLAP_WINDOW_MS=900
CLAP_REFRACTORY_MS=100
CLAP_MAX_MS=60
```
(Same clap tuning values already validated on the PC — start here,
re-tune only if the room/mic behaves differently.)

## 7. Build and run

```bash
cd ~/TANY-home/homebot/capture-svc
docker compose -f docker-compose.pi.yml --env-file ../.env up -d --build
docker compose -f docker-compose.pi.yml logs -f
```
Expect to see the same startup lines as on the PC (`clap detector
config...`, `mic input: device=...`, `capture-svc up. listening for
double-clap...`). `Ctrl+C` to stop following logs (the container keeps
running).

## 8. Verify

- Dashboard (from any other device on the LAN/Tailscale):
  `http://homebot-pi.local:8010` — mic-requiring features (voice-id
  capture) need HTTPS to work remotely; see the PC's Tailscale cert
  setup for the pattern if you need that here too, not required just
  to confirm the Pi is alive.
- Clap near the Pi → should hear the greeting through the BT speaker
  and be able to talk, no browser involved anywhere.
- Check `docker compose -f docker-compose.pi.yml logs -f` for
  `trigger detected (rtsp) -> triggering identification`.

## Once confirmed working

Stop capture-svc on the PC so the two don't both listen for claps
against the same RTSP camera/orchestrator at once (same restart-loop
class of bug already fixed once this session) — the Pi becomes the one
physical listener from here on.

## Step 3 preview (physical button)

Uncomment `/dev/gpiomem:/dev/gpiomem` and `BUTTON_GPIO_PIN=17` (or your
actual pin) in `docker-compose.pi.yml`, wire the button, `docker compose
-f docker-compose.pi.yml up -d --build` again. `gpiozero` needs a pin
factory library (`lgpio` is preinstalled on current Raspberry Pi OS but
not necessarily inside this slim container) — if the button doesn't
register, check the container logs for the specific import error first.
