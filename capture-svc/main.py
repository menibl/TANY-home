import asyncio
import logging
import os
import ssl

import numpy as np
import requests
import sounddevice as sd
import urllib3
import websockets

# expected/intentional (see say()'s verify=False) — silence the noise
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from clap_detector import DoubleClapDetector
from rtsp_snapshot import grab_snapshots, grab_webcam_snapshots

logging.basicConfig(level=logging.INFO, format="%(asctime)s [capture-svc] %(message)s")
log = logging.getLogger(__name__)

RTSP_URL = os.environ["RTSP_URL"]
VISION_SVC_URL = os.environ.get("VISION_SVC_URL", "http://vision-id-svc:8001")
VOICE_SVC_URL = os.environ.get("VOICE_SVC_URL", "http://voice-id-svc:8002")
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8004")
WEB_UI_PORT = int(os.environ.get("WEB_UI_PORT", "8010"))
# Optional physical trigger button (e.g. on a Pi with GPIO), alongside the
# clap detector. Unset on any machine without one (PC dev box, this
# machine, ...) -> the GPIO listener below is simply never started.
BUTTON_GPIO_PIN = os.environ.get("BUTTON_GPIO_PIN")
# When on: the live conversation is native OpenAI Realtime speech-to-
# speech, browser<->OpenAI directly over WebRTC (see orchestrator/
# realtime.py and static/dashboard.html) instead of the Claude cascaded
# pipeline (capture-svc mic -> orchestrator STT -> Claude -> TTS ->
# capture-svc speaker). Much lower latency; costs Claude as the model.
USE_REALTIME = os.environ.get("USE_REALTIME", "0") == "1"
SAMPLE_RATE = 16000
# ALSA device names to use explicitly instead of relying on PortAudio's
# "default" resolution — see _select_mic_stream_params for why. Match
# the pcm names in ~/.asoundrc (e.g. "m5mic_plug" / "jbl_a2dp" on the
# Pi with the M5StickC mic + JBL speaker). Leave unset to keep using
# whatever device PortAudio picks as default (fine on Windows/normal
# setups where that resolution works correctly).
MIC_DEVICE_NAME = os.environ.get("MIC_DEVICE_NAME")
SPEAKER_DEVICE_NAME = os.environ.get("SPEAKER_DEVICE_NAME")
FRAME_MS = 20
# Must match orchestrator/speech_io.py's TTS_SAMPLE_RATE (OpenAI `pcm`
# responses are fixed at 24kHz) — raw PCM carries no header to read this from.
TTS_SAMPLE_RATE = 24000

detector = DoubleClapDetector(
    sample_rate=SAMPLE_RATE,
    frame_ms=FRAME_MS,
    energy_threshold=float(os.environ.get("CLAP_ENERGY_THRESHOLD", "0.35")),
    clap_window_ms=int(os.environ.get("CLAP_WINDOW_MS", "600")),
    refractory_ms=int(os.environ.get("CLAP_REFRACTORY_MS", "250")),
    max_clap_ms=int(os.environ.get("CLAP_MAX_MS", "60")),
)
log.info("clap detector config: threshold=%.3f window_ms=%d refractory_ms=%d max_clap_ms=%d",
          detector.energy_threshold, detector.clap_window_ms, detector.refractory_ms,
          detector.max_clap_frames * FRAME_MS)

import web_ui

# Mic hand-off between the always-on clap listener and one-off mic users
# (the live conversation, and enrollment voice recording): only one
# open_mic_stream() may be open at a time (see open_conversation_session
# for why), so these coordinate who currently owns the device.
mic_yield_requested = asyncio.Event()
mic_yielded = asyncio.Event()
mic_resume = asyncio.Event()

# Set by the dashboard's "click the orb" button — same trigger as a real
# double-clap, except identification uses the local webcam instead of the
# RTSP camera (the whole point of a clap trigger is not being at the
# computer; a click means you obviously are).
manual_trigger_event = asyncio.Event()

# Set by a physical GPIO button press (see BUTTON_GPIO_PIN). Treated
# exactly like a real double-clap (RTSP snapshot, not webcam) since the
# button lives wherever the mic/camera do, same as clapping.
button_trigger_event = asyncio.Event()

# Set by the dashboard's "סיים שיחה" button to end the active
# conversation on demand instead of waiting for the orchestrator to.
end_session_event = asyncio.Event()


def set_status(state: str, text: str = ""):
    web_ui.broadcast_status(state, text)


def _select_mic_stream_params():
    """Prefer a WASAPI input device at its own native sample rate.
    Opening the default device directly at 16kHz (MME on Windows) was
    observed to produce periodic buffer glitches that read as false
    high-energy spikes and break clap detection entirely. Falls back to
    the default device at SAMPLE_RATE when no WASAPI device exists
    (e.g. Linux, where this problem doesn't happen)."""
    if MIC_DEVICE_NAME:
        # explicit override, by name — PortAudio's own "default" device
        # resolution doesn't reliably follow ~/.asoundrc's pcm.!default
        # (confirmed live on the Pi: sd.default.device pointed at a raw
        # hw:X,Y ALSA loopback device instead of the named "asym"
        # default we configured), so name the real device directly
        # instead of fighting that resolution.
        try:
            for i, dev in enumerate(sd.query_devices()):
                if dev["name"] == MIC_DEVICE_NAME and dev["max_input_channels"] > 0:
                    return i, int(dev["default_samplerate"])
            log.warning("MIC_DEVICE_NAME=%s not found among input devices, falling back", MIC_DEVICE_NAME)
        except Exception:
            log.exception("MIC_DEVICE_NAME=%s lookup failed, falling back", MIC_DEVICE_NAME)
    try:
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                hostapi = sd.query_hostapis(dev["hostapi"])["name"]
                if "WASAPI" in hostapi:
                    return i, int(dev["default_samplerate"])
    except Exception:
        log.exception("WASAPI device lookup failed, using default input device")
    return None, SAMPLE_RATE


_MIC_DEVICE, _MIC_NATIVE_RATE = _select_mic_stream_params()
_DOWNSAMPLE_FACTOR = max(1, _MIC_NATIVE_RATE // SAMPLE_RATE)
log.info("mic input: device=%s native_rate=%d downsample_factor=%d",
          _MIC_DEVICE, _MIC_NATIVE_RATE, _DOWNSAMPLE_FACTOR)


def _downsample(frame: np.ndarray) -> np.ndarray:
    if _DOWNSAMPLE_FACTOR == 1:
        return frame
    n = (len(frame) // _DOWNSAMPLE_FACTOR) * _DOWNSAMPLE_FACTOR
    return frame[:n].reshape(-1, _DOWNSAMPLE_FACTOR).mean(axis=1).astype(np.int16)


def open_mic_stream(frame_callback):
    """Opens an input stream that always delivers SAMPLE_RATE (16kHz)
    int16 mono frames to frame_callback(frame: np.ndarray), regardless
    of the underlying device's native rate."""
    native_frame_size = int(_MIC_NATIVE_RATE * FRAME_MS / 1000)

    def _cb(indata, frames, time_info, status):
        raw = np.frombuffer(bytes(indata), dtype=np.int16)
        frame_callback(_downsample(raw))

    return sd.RawInputStream(
        samplerate=_MIC_NATIVE_RATE,
        blocksize=native_frame_size,
        dtype="int16",
        channels=1,
        device=_MIC_DEVICE,
        callback=_cb,
    )


def start_button_listener(loop: asyncio.AbstractEventLoop):
    """No-op unless BUTTON_GPIO_PIN is set. gpiozero itself imports fine
    on any platform; it's instantiating Button() without a real GPIO chip
    behind it that fails, so that only happens when a pin was explicitly
    configured -- a PC/dev box that never sets BUTTON_GPIO_PIN never
    touches gpiozero at all."""
    if not BUTTON_GPIO_PIN:
        return
    try:
        from gpiozero import Button
        button = Button(int(BUTTON_GPIO_PIN), bounce_time=0.2)
        button.when_pressed = lambda: loop.call_soon_threadsafe(button_trigger_event.set)
        log.info("physical button listener active on GPIO%s", BUTTON_GPIO_PIN)
    except Exception:
        log.exception("BUTTON_GPIO_PIN=%s set but button init failed -- physical button disabled", BUTTON_GPIO_PIN)


def identify_from_snapshot(source: str = "rtsp") -> dict:
    """Stage 1 -> Stage 2 handoff. Blocking on purpose — this happens
    once per trigger, not in the audio hot path. `source` is "rtsp" for
    a real clap or "webcam" for the dashboard's manual trigger button."""
    if source == "webcam":
        frames = grab_webcam_snapshots(num_frames=2)
    else:
        frames = grab_snapshots(RTSP_URL, num_frames=2)
    resp = requests.post(
        f"{VISION_SVC_URL}/identify",
        json={"image_b64": frames[0]},
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json()


def play_pcm16(audio_bytes: bytes, sample_rate: int):
    """OpenAI's `pcm` TTS output comes back at a few percent of full
    scale (near-inaudible on real speakers), so normalize to a sane
    peak before playing."""
    if not audio_bytes:
        return
    samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float64)
    peak = np.abs(samples).max()
    duration_s = len(samples) / sample_rate
    log.info("DEBUG: play_pcm16 samples=%d peak=%.0f duration=%.2fs rate=%d", len(samples), peak, duration_s, sample_rate)
    if peak > 0:
        samples = samples / peak * 30000
    sd.play(samples.astype(np.int16), samplerate=sample_rate, device=SPEAKER_DEVICE_NAME, blocking=True)
    log.info("DEBUG: sd.play() returned")


async def say(text: str):
    """Fetches TTS audio for the one-off greeting from the orchestrator
    and plays it through the local speaker. Kept as a separate function
    so swapping TTS engines never touches the trigger/identification
    logic above it — the engine itself lives in orchestrator/speech_io.py."""
    log.info("TTS -> %r", text)
    try:
        # orchestrator serves HTTPS-only whenever a Tailscale cert is
        # mounted (see docker-compose.yml) — that cert is issued for the
        # PC's Tailscale hostname, not whatever address ORCHESTRATOR_URL
        # actually points at here (a LAN IP, from e.g. the Pi), so
        # hostname verification would always fail even though this is
        # our own known server. Machine-to-machine on a home LAN, not a
        # browser — skipping verification is the standard tradeoff here.
        resp = await asyncio.to_thread(
            requests.post, f"{ORCHESTRATOR_URL}/tts", json={"text": text}, timeout=15,
            verify=False,
        )
        resp.raise_for_status()
        rate = int(resp.headers.get("X-Sample-Rate", TTS_SAMPLE_RATE))
        await asyncio.to_thread(play_pcm16, resp.content, rate)
    except Exception:
        log.exception("TTS playback failed")


async def open_conversation_session(user_id: str | None, certain: bool):
    """Stage 4 handoff: opens the streaming session with the orchestrator
    and pumps mic audio in / speaker audio out until the conversation ends."""
    query = f"user_id={user_id or ''}&certain={'true' if certain else 'false'}"
    uri = ORCHESTRATOR_URL.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{uri}/session?{query}"

    log.info("opening conversation session: certain=%s user_id=%s", certain, user_id)

    loop = asyncio.get_running_loop()

    # ping_interval=None: a single turn (whisper STT + Claude + TTS) can
    # take well over the default 20s ping/pong keepalive tolerance on a
    # slow turn, which was killing the connection right as the reply
    # became ready. This is a local connection to a container on the
    # same machine — dead-connection detection isn't needed here.
    #
    # same cert-hostname mismatch as say()'s verify=False above — wss://
    # here needs an SSL context that skips hostname/cert verification.
    ssl_context = None
    if uri.startswith("wss://"):
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    async with websockets.connect(uri, max_size=None, ping_interval=None, ssl=ssl_context) as ws:
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue()
        # True half-duplex, not just software muting: some Bluetooth
        # audio hardware (e.g. a cheap HFP speaker/mic combo) can't
        # reliably run capture and playback at once even over the same
        # SCO link — muting by dropping frames still left the underlying
        # capture stream physically open and fighting the playback
        # stream for the link. Physically stop/close the mic stream
        # while playing a reply, then reopen it once done.
        mic_state = {"stream": None}

        def mic_callback(frame: np.ndarray):
            # runs on PortAudio's own thread, not the event loop thread —
            # asyncio.Queue isn't thread-safe, so hop back via call_soon_threadsafe.
            loop.call_soon_threadsafe(mic_queue.put_nowait, frame.tobytes())

        def start_mic():
            if mic_state["stream"] is None:
                s = open_mic_stream(mic_callback)
                s.start()
                mic_state["stream"] = s

        def stop_mic():
            s = mic_state["stream"]
            if s is not None:
                mic_state["stream"] = None
                s.stop()
                s.close()

        start_mic()

        async def send_mic_audio():
            while True:
                chunk = await mic_queue.get()
                await ws.send(chunk)

        async def receive_events():
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    # full-reply TTS audio from orchestrator (see /session
                    # in orchestrator/main.py) -> play it through the speaker
                    log.info("DEBUG: audio message received, len=%d bytes", len(message))
                    set_status("speaking")
                    log.info("DEBUG: stopping mic")
                    await asyncio.to_thread(stop_mic)
                    log.info("DEBUG: mic stopped, calling play_pcm16")
                    try:
                        await asyncio.to_thread(play_pcm16, bytes(message), TTS_SAMPLE_RATE)
                        log.info("DEBUG: play_pcm16 returned normally")
                    except Exception:
                        log.exception("DEBUG: play_pcm16 raised")
                    finally:
                        log.info("DEBUG: restarting mic")
                        await asyncio.to_thread(start_mic)
                        log.info("DEBUG: mic restarted")
                    continue
                import json
                event = json.loads(message)
                etype = event.get("type")
                if etype == "identity_update":
                    log.info("identity confirmed mid-conversation: %s (%.2f)",
                              event["user_id"], event["confidence"])
                elif etype == "partial_transcript":
                    set_status("heard", event["text"])
                elif etype == "tool_call":
                    set_status("thinking", event["name"])
                elif etype == "end_of_turn":
                    set_status("session")
                elif etype == "session_end":
                    log.info("session ended by orchestrator")
                    return

        mic_task = asyncio.create_task(send_mic_audio())
        events_task = asyncio.create_task(receive_events())
        end_task = asyncio.create_task(end_session_event.wait())

        done, pending = await asyncio.wait(
            {mic_task, events_task, end_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if end_task in done:
            end_session_event.clear()
            log.info("session ended by user (dashboard button)")
        else:
            end_task.cancel()

        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        stop_mic()
        # a real failure in the mic/events pump (not our own cancellation)
        # should still surface to main_loop's outer try/except and get logged
        for t in done:
            if t is not end_task:
                await t


async def main_loop():
    log.info("capture-svc up. listening for double-clap...")
    loop = asyncio.get_running_loop()
    trigger_event = asyncio.Event()
    start_button_listener(loop)

    def audio_callback(frame: np.ndarray):
        # runs on PortAudio's own thread, not the event loop thread —
        # asyncio.Event isn't thread-safe, so hop back via call_soon_threadsafe
        if detector.process_frame(frame):
            loop.call_soon_threadsafe(trigger_event.set)

    while True:
        set_status("listening")
        # only held open while waiting for the clap — held open through the
        # whole conversation too, it fights the session's own mic stream for
        # the same device and both end up starved (near-silent audio in)
        with open_mic_stream(audio_callback):
            wait_clap = asyncio.create_task(trigger_event.wait())
            wait_manual = asyncio.create_task(manual_trigger_event.wait())
            wait_button = asyncio.create_task(button_trigger_event.wait())
            wait_yield = asyncio.create_task(mic_yield_requested.wait())
            done, pending = await asyncio.wait(
                {wait_clap, wait_manual, wait_button, wait_yield}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

        if wait_yield in done:
            # enrollment (or something else) needs the mic — release ours,
            # wait for it to finish, then go back to listening for claps
            mic_yielded.set()
            await mic_resume.wait()
            mic_resume.clear()
            mic_yielded.clear()
            continue

        if wait_manual in done:
            manual_trigger_event.clear()
            snapshot_source = "webcam"
        elif wait_button in done:
            button_trigger_event.clear()
            snapshot_source = "rtsp"
        else:
            trigger_event.clear()
            snapshot_source = "rtsp"

        log.info("trigger detected (%s) -> triggering identification", snapshot_source)
        set_status("clap")
        try:
            set_status("identifying")
            result = identify_from_snapshot(source=snapshot_source)
        except Exception:
            log.exception("snapshot/identify failed, greeting generically")
            result = {"certain": False, "best_guess": None}

        user_id = result.get("best_guess")

        if USE_REALTIME:
            # the browser (dashboard.html) takes it from here — opens the
            # WebRTC session straight to OpenAI and handles the whole
            # conversation itself, so capture-svc doesn't block; it just
            # hands off who's talking and goes back to listening. No
            # say() here: the greeting is now the Realtime session's own
            # first turn (see orchestrator/realtime.py) so the whole
            # conversation is one continuous voice instead of switching
            # from the old TTS pipeline partway through.
            set_status("greeting")
            web_ui.broadcast_status(
                "realtime_start", "",
                user_id=user_id, certain=bool(result.get("certain")),
            )
            # main_loop must NOT go back to listening (and reopen its own
            # mic stream) until the browser's WebRTC session actually
            # ends — otherwise capture-svc's clap detector runs the whole
            # time the realtime conversation is live, and short loud
            # bursts in normal speech (which pass the same duration gate
            # as a real clap) fire a brand new session on top of the
            # current one. dashboard.html POSTs /api/end-session from
            # every exit path (silence timeout, disconnect, manual end,
            # the end_conversation tool) via stopRealtimeSession().
            end_session_event.clear()
            await end_session_event.wait()
            end_session_event.clear()
        else:
            set_status("greeting")
            if result.get("certain"):
                await say(f"שלום {user_id}")
            else:
                await say("שלום")
            set_status("session")
            try:
                await open_conversation_session(user_id, certain=bool(result.get("certain")))
            except Exception:
                # a dropped connection mid-conversation must not take down
                # the whole process (including the web dashboard) — log it
                # and go back to listening for the next clap instead
                log.exception("conversation session ended abnormally")
        log.info("back to listening for double-clap...")


async def run_all():
    import uvicorn

    web_ui.configure(
        rtsp_url=RTSP_URL,
        vision_svc_url=VISION_SVC_URL,
        voice_svc_url=VOICE_SVC_URL,
        open_mic_stream=open_mic_stream,
        grab_snapshots=grab_snapshots,
        sample_rate=SAMPLE_RATE,
        mic_yield_requested=mic_yield_requested,
        mic_yielded=mic_yielded,
        mic_resume=mic_resume,
        manual_trigger_event=manual_trigger_event,
        end_session_event=end_session_event,
    )
    # 0.0.0.0, not 127.0.0.1: lets other devices on the LAN/Tailscale
    # reach the dashboard too (e.g. answering a triggered conversation
    # from a phone). The clap/camera trigger itself still only ever uses
    # this machine's own mic/RTSP feed — that part doesn't move.
    #
    # getUserMedia (the browser mic, needed for the realtime conversation)
    # only works in a "secure context" — https://, or literally
    # localhost/127.0.0.1. A bare LAN/Tailscale IP over http never
    # qualifies, so from any other device the realtime path silently
    # breaks unless we actually serve HTTPS. `tailscale cert` (see
    # README) writes tailscale.crt/.key here when available — use them
    # if present, otherwise fall back to plain HTTP (still fine for
    # 127.0.0.1 access on this machine).
    # shared with orchestrator's container via a bind mount (see
    # docker-compose.yml) so both sides of every browser call are HTTPS
    # under the same Tailscale hostname
    certs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "certs")
    cert_file = os.path.join(certs_dir, "tailscale.crt")
    key_file = os.path.join(certs_dir, "tailscale.key")
    if os.path.exists(cert_file) and os.path.exists(key_file):
        config = uvicorn.Config(
            web_ui.app, host="0.0.0.0", port=WEB_UI_PORT, log_level="warning",
            ssl_certfile=cert_file, ssl_keyfile=key_file,
        )
        log.info("dashboard: https://127.0.0.1:%d (Tailscale cert found — also reachable via your Tailscale hostname)", WEB_UI_PORT)
    else:
        config = uvicorn.Config(web_ui.app, host="0.0.0.0", port=WEB_UI_PORT, log_level="warning")
        log.info("dashboard: http://127.0.0.1:%d (no Tailscale cert found — realtime conversation won't work from other devices)", WEB_UI_PORT)
    server = uvicorn.Server(config)
    await asyncio.gather(main_loop(), server.serve())


if __name__ == "__main__":
    asyncio.run(run_all())
