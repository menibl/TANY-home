"""
STT/TTS live here as swappable functions, not baked into main.py, so you
can move from faster-whisper -> a hosted STT, or OpenAI TTS -> Piper/
ElevenLabs, without touching the session loop.
"""
import os

import numpy as np
from faster_whisper import WhisperModel
from openai import OpenAI

# Measured live: "small" took ~8s to transcribe a 3s Hebrew utterance on
# this machine, which was most of the end-to-end reply latency. "base"
# trades some accuracy for real-time-flowing conversation. Swap to
# "small"/"medium" if this container gets relocated to a beefier machine.
_STT_MODEL_SIZE = os.environ.get("STT_MODEL_SIZE", "base")
_stt_model = WhisperModel(_STT_MODEL_SIZE, device="cpu", compute_type="int8")

# Contract with capture-svc: synthesize() always returns raw PCM16 mono
# at this rate (OpenAI's `pcm` response format is fixed at 24kHz) — the
# player on the other end must know the rate up front since raw PCM
# carries no header.
TTS_SAMPLE_RATE = 24000
_TTS_VOICE = os.environ.get("TTS_VOICE", "alloy")
_TTS_MODEL = os.environ.get("TTS_MODEL", "tts-1")

_openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def transcribe(pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
    audio = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = _stt_model.transcribe(
        audio,
        language="he",
        # Measured live: on longer/noisier utterances, faster-whisper would
        # occasionally fall into a repetition loop ("מה שעבית? מה שעבית?"
        # over and over) that took 40-75s instead of 2-3s.
        # condition_on_previous_text=False stops one bad segment from
        # poisoning the next. (Also tried vad_filter=True to pre-strip
        # silence, but it hung a live session for 40s+ on first use —
        # not worth the risk here, dropped.)
        condition_on_previous_text=False,
    )
    return "".join(seg.text for seg in segments).strip()


def synthesize(text: str) -> bytes:
    """OpenAI TTS -> raw PCM16 mono @ TTS_SAMPLE_RATE. Swap to Piper
    (local, free) or ElevenLabs here later without touching callers."""
    if not text.strip():
        return b""
    response = _openai_client.audio.speech.create(
        model=_TTS_MODEL,
        voice=_TTS_VOICE,
        input=text,
        response_format="pcm",
    )
    return response.read()


class SilenceBasedEndpointer:
    """Very small VAD: flags end-of-utterance after N ms of low energy
    following at least some speech. Good enough to start; swap for
    webrtcvad/silero-vad later if barge-in handling needs to be tighter."""

    def __init__(self, sample_rate=16000, silence_ms=None, energy_threshold=None, max_utterance_ms=None):
        silence_ms = silence_ms if silence_ms is not None else int(os.environ.get("ENDPOINT_SILENCE_MS", "700"))
        energy_threshold = energy_threshold if energy_threshold is not None else float(os.environ.get("ENDPOINT_ENERGY_THRESHOLD", "0.04"))
        max_utterance_ms = max_utterance_ms if max_utterance_ms is not None else int(os.environ.get("ENDPOINT_MAX_UTTERANCE_MS", "8000"))
        self.sample_rate = sample_rate
        self.silence_frames_needed = silence_ms // 20
        self.energy_threshold = energy_threshold
        # A long open-ended utterance (background noise keeping "speech"
        # alive, or someone just talking a while) is exactly what pushed
        # whisper into the repetition-loop failure mode above — force a
        # cut once it's been going this long, whether or not it's paused.
        self.max_frames = max_utterance_ms // 20
        self._silence_run = 0
        self._heard_speech = False
        self._speech_frames = 0
        self._buffer = bytearray()

    def push(self, frame: bytes) -> bytes | None:
        """Feed a ~20ms PCM16 frame. Returns the accumulated utterance
        bytes once a full utterance has ended, else None."""
        self._buffer.extend(frame)
        arr = np.frombuffer(frame, dtype=np.int16).astype(np.float64)
        energy = np.sqrt(np.mean(arr ** 2)) / 32768.0

        if energy > self.energy_threshold:
            self._heard_speech = True
            self._silence_run = 0
        else:
            self._silence_run += 1
        if self._heard_speech:
            self._speech_frames += 1

        cut = self._heard_speech and (
            self._silence_run >= self.silence_frames_needed
            or self._speech_frames >= self.max_frames
        )
        if cut:
            utterance = bytes(self._buffer)
            self._buffer.clear()
            self._heard_speech = False
            self._silence_run = 0
            self._speech_frames = 0
            return utterance
        return None
