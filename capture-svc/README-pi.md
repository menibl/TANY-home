# capture-svc on Raspberry Pi (headless, BT speaker+mic)

Scope of this move: **only capture-svc** runs on the Pi. Every other
service (orchestrator, vision-id-svc, voice-id-svc, tany-bridge,
profile-store) stays on the PC, exactly as it runs today — the Pi just
becomes the physical presence (mic/speaker/camera-trigger/button) and
talks to the PC over the LAN. Face identification still uses the RTSP
camera already configured in `.env`, not anything attached to the Pi.

Replace `<PC_LAN_IP>` below with this PC's LAN IP: **192.168.68.58**
(confirm with `ipconfig` on the PC if it's changed since).

## 0. Before you start — what you need (Pi 3 B+)

- The Pi 3 B+ board itself, and the microSD card + USB card reader you
  already have.
- **A micro-USB power supply, 5V/2.5A** — the Pi 3 B+ uses micro-USB,
  *not* the USB-C connector Pi 4/5 use. A phone charger that isn't
  rated for at least 2.5A will cause random reboots/instability under
  load — worth checking the label, don't guess.
- Either a WiFi network to join, or an Ethernet cable to your router —
  both work, pick one; instructions below cover both.
- Bluetooth is **built into** the Pi 3 B+ (no USB dongle needed) — that's
  what the BT speaker/mic will pair over.
- **Only 1GB RAM** on this model — not a blocker for capture-svc (it's
  a light service; the heavy stuff — Whisper, face/voice matching —
  stays on the PC), but Docker image builds can be slow, and the
  default swap file is small enough that a build could fail outright.
  Step 2 below includes bumping it before you build anything.

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

1. Download **Raspberry Pi Imager** from
   https://www.raspberrypi.com/software/ (the Windows `.exe`) and
   install it on this PC. Plug the microSD card into the USB reader.
2. Open Raspberry Pi Imager:
   - **Choose Device** → Raspberry Pi 3
   - **Choose OS** → "Raspberry Pi OS (other)" → **Raspberry Pi OS
     Lite (64-bit)** (no desktop needed — this is a headless box)
   - **Choose Storage** → your microSD card (double-check it's the
     right drive, this erases it)
3. Click the gear icon (⚙, bottom right — "Edit Settings") before
   writing:
   - **General tab**: set hostname to `homebot-pi`; tick "Enable SSH",
     choose password auth, set a username/password you'll remember;
     if using WiFi, tick "Configure wireless LAN" and enter your
     SSID/password (skip this if using an Ethernet cable instead)
   - **Services tab**: confirm SSH is enabled
   - Save, then click **Write** and confirm. Takes a few minutes.
4. Move the microSD card into the Pi, connect the Ethernet cable (if
   using one), then connect power last. Wait ~2 minutes for first boot
   (it reboots once automatically partway through).
5. From this PC:
   ```bash
   ssh homebot@homebot-pi.local
   ```
   (use whatever username you set above). If `.local` doesn't resolve,
   find its IP from your router's connected-devices list instead and
   `ssh homebot@<that IP>`.

## 2. Update, bump swap, install Docker

Only 1GB RAM on this board — the default 100MB swap file is too small
for a Docker build (installing numpy/opencv can need more headroom
than that). Bump it before building anything:
```bash
sudo apt update && sudo apt full-upgrade -y
sudo dphys-swapfile swapoff
sudo sed -i 's/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
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
