import asyncio
import logging
import os
import threading

import numpy as np
import requests
import sounddevice as sd
import websockets

from clap_detector import DoubleClapDetector
from rtsp_snapshot import grab_snapshots

logging.basicConfig(level=logging.INFO, format="%(asctime)s [capture-svc] %(message)s")
log = logging.getLogger(__name__)

RTSP_URL = os.environ["RTSP_URL"]
VISION_SVC_URL = os.environ.get("VISION_SVC_URL", "http://vision-id-svc:8001")
VOICE_SVC_URL = os.environ.get("VOICE_SVC_URL", "http://voice-id-svc:8002")
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8004")
WEB_UI_PORT = int(os.environ.get("WEB_UI_PORT", "8010"))
SAMPLE_RATE = 16000
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
)
log.info("clap detector config: threshold=%.3f window_ms=%d refractory_ms=%d",
          detector.energy_threshold, detector.clap_window_ms, detector.refractory_ms)

import web_ui

# Mic hand-off between the always-on clap listener and one-off mic users
# (the live conversation, and enrollment voice recording): only one
# open_mic_stream() may be open at a time (see open_conversation_session
# for why), so these coordinate who currently owns the device.
mic_yield_requested = asyncio.Event()
mic_yielded = asyncio.Event()
mic_resume = asyncio.Event()


def set_status(state: str, text: str = ""):
    web_ui.broadcast_status(state, text)


def _select_mic_stream_params():
    """Prefer a WASAPI input device at its own native sample rate.
    Opening the default device directly at 16kHz (MME on Windows) was
    observed to produce periodic buffer glitches that read as false
    high-energy spikes and break clap detection entirely. Falls back to
    the default device at SAMPLE_RATE when no WASAPI device exists
    (e.g. Linux, where this problem doesn't happen)."""
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


def identify_from_snapshot() -> dict:
    """Stage 1 -> Stage 2 handoff. Blocking on purpose — this happens
    once per trigger, not in the audio hot path."""
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
    if peak > 0:
        samples = samples / peak * 30000
    sd.play(samples.astype(np.int16), samplerate=sample_rate, blocking=True)


async def say(text: str):
    """Fetches TTS audio for the one-off greeting from the orchestrator
    and plays it through the local speaker. Kept as a separate function
    so swapping TTS engines never touches the trigger/identification
    logic above it — the engine itself lives in orchestrator/speech_io.py."""
    log.info("TTS -> %r", text)
    try:
        resp = await asyncio.to_thread(
            requests.post, f"{ORCHESTRATOR_URL}/tts", json={"text": text}, timeout=15
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
    async with websockets.connect(uri, max_size=None, ping_interval=None) as ws:
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue()
        speaker_active = threading.Event()

        def mic_callback(frame: np.ndarray):
            # runs on PortAudio's own thread, not the event loop thread —
            # asyncio.Queue isn't thread-safe, so hop back via call_soon_threadsafe.
            # Drop frames while TTS is playing — without this the mic picks
            # up the bot's own reply through the speaker and sends it back
            # as if it were the user talking, triggering an echo of Claude
            # calls answering itself.
            if speaker_active.is_set():
                return
            loop.call_soon_threadsafe(mic_queue.put_nowait, frame.tobytes())

        stream = open_mic_stream(mic_callback)

        async def send_mic_audio():
            with stream:
                while True:
                    chunk = await mic_queue.get()
                    await ws.send(chunk)

        async def receive_events():
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    # full-reply TTS audio from orchestrator (see /session
                    # in orchestrator/main.py) -> play it through the speaker
                    set_status("speaking")
                    speaker_active.set()
                    try:
                        await asyncio.to_thread(play_pcm16, bytes(message), TTS_SAMPLE_RATE)
                    finally:
                        speaker_active.clear()
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

        await asyncio.gather(send_mic_audio(), receive_events())


async def main_loop():
    log.info("capture-svc up. listening for double-clap...")
    loop = asyncio.get_running_loop()
    trigger_event = asyncio.Event()

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
            wait_yield = asyncio.create_task(mic_yield_requested.wait())
            done, pending = await asyncio.wait(
                {wait_clap, wait_yield}, return_when=asyncio.FIRST_COMPLETED
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

        trigger_event.clear()
        log.info("double-clap detected -> triggering identification")
        set_status("clap")
        try:
            set_status("identifying")
            result = identify_from_snapshot()
        except Exception:
            log.exception("snapshot/identify failed, greeting generically")
            result = {"certain": False, "best_guess": None}

        set_status("greeting")
        if result.get("certain"):
            user_id = result["best_guess"]
            await say(f"שלום {user_id}")
        else:
            user_id = result.get("best_guess")
            await say("שלום")

        set_status("session")
        try:
            await open_conversation_session(user_id, certain=bool(result.get("certain")))
        except Exception:
            # a dropped connection mid-conversation must not take down the
            # whole process (including the web dashboard) — log it and go
            # back to listening for the next clap instead
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
    )
    config = uvicorn.Config(web_ui.app, host="127.0.0.1", port=WEB_UI_PORT, log_level="warning")
    server = uvicorn.Server(config)
    log.info("dashboard: http://127.0.0.1:%d", WEB_UI_PORT)
    await asyncio.gather(main_loop(), server.serve())


if __name__ == "__main__":
    asyncio.run(run_all())
