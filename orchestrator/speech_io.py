"""
STT/TTS live here as swappable functions, not baked into main.py, so you
can move from faster-whisper -> a hosted STT, or OpenAI TTS -> Piper/
ElevenLabs, without touching the session loop.
"""
import os

import numpy as np
from faster_whisper import WhisperModel
from openai import OpenAI

# "small" is a reasonable CPU-only accuracy/speed tradeoff for Hebrew.
# Swap to "medium" if this container gets relocated to a beefier machine.
_stt_model = WhisperModel("small", device="cpu", compute_type="int8")

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
    segments, _ = _stt_model.transcribe(audio, language="he")
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

    def __init__(self, sample_rate=16000, silence_ms=700, energy_threshold=0.02):
        self.sample_rate = sample_rate
        self.silence_frames_needed = silence_ms // 20
        self.energy_threshold = energy_threshold
        self._silence_run = 0
        self._heard_speech = False
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

        if self._heard_speech and self._silence_run >= self.silence_frames_needed:
            utterance = bytes(self._buffer)
            self._buffer.clear()
            self._heard_speech = False
            self._silence_run = 0
            return utterance
        return None
