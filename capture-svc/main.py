import asyncio
import base64
import logging
import os

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
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8004")
SAMPLE_RATE = 16000
FRAME_MS = 20

detector = DoubleClapDetector(
    sample_rate=SAMPLE_RATE,
    frame_ms=FRAME_MS,
    energy_threshold=float(os.environ.get("CLAP_ENERGY_THRESHOLD", "0.35")),
    clap_window_ms=int(os.environ.get("CLAP_WINDOW_MS", "600")),
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


def say(text: str):
    """Placeholder TTS hook — wire up Piper/edge-tts/ElevenLabs here.
    Kept as a separate function so swapping TTS engines never touches
    the trigger/identification logic above it."""
    log.info("TTS -> %r", text)
    # TODO: synthesize `text` and play through the local speaker


async def open_conversation_session(user_id: str | None, certain: bool):
    """Stage 4 handoff: opens the streaming session with the orchestrator
    and pumps mic audio in / speaker audio out until the conversation ends."""
    query = f"user_id={user_id or ''}&certain={'true' if certain else 'false'}"
    uri = ORCHESTRATOR_URL.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{uri}/session?{query}"

    log.info("opening conversation session: certain=%s user_id=%s", certain, user_id)

    async with websockets.connect(uri, max_size=None) as ws:
        mic_queue: asyncio.Queue[bytes] = asyncio.Queue()

        def mic_callback(indata, frames, time_info, status):
            mic_queue.put_nowait(bytes(indata))

        stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=int(SAMPLE_RATE * FRAME_MS / 1000),
            dtype="int16",
            channels=1,
            callback=mic_callback,
        )

        async def send_mic_audio():
            with stream:
                while True:
                    chunk = await mic_queue.get()
                    await ws.send(chunk)

        async def receive_events():
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    # raw TTS audio chunk from orchestrator -> play it
                    # TODO: feed to speaker output stream
                    continue
                import json
                event = json.loads(message)
                etype = event.get("type")
                if etype == "identity_update":
                    log.info("identity confirmed mid-conversation: %s (%.2f)",
                              event["user_id"], event["confidence"])
                elif etype == "end_of_turn":
                    pass  # hook for UI/state if needed
                elif etype == "session_end":
                    log.info("session ended by orchestrator")
                    return

        await asyncio.gather(send_mic_audio(), receive_events())


async def main_loop():
    log.info("capture-svc up. listening for double-clap...")
    frame_size = int(SAMPLE_RATE * FRAME_MS / 1000)

    trigger_event = asyncio.Event()

    def audio_callback(indata, frames, time_info, status):
        frame = np.frombuffer(bytes(indata), dtype=np.int16)
        if detector.process_frame(frame):
            trigger_event.set()

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=frame_size,
        dtype="int16",
        channels=1,
        callback=audio_callback,
    ):
        while True:
            await trigger_event.wait()
            trigger_event.clear()
            log.info("double-clap detected -> triggering identification")
            try:
                result = identify_from_snapshot()
            except Exception:
                log.exception("snapshot/identify failed, greeting generically")
                result = {"certain": False, "best_guess": None}

            if result.get("certain"):
                user_id = result["best_guess"]
                say(f"שלום {user_id}")
            else:
                user_id = result.get("best_guess")
                say("שלום")

            await open_conversation_session(user_id, certain=bool(result.get("certain")))
            log.info("back to listening for double-clap...")


if __name__ == "__main__":
    asyncio.run(main_loop())
