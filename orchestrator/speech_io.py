"""
STT/TTS live here as swappable functions, not baked into main.py, so you
can move from faster-whisper -> a hosted STT, or Piper -> ElevenLabs,
without touching the session loop.
"""
import io
import wave

import numpy as np
from faster_whisper import WhisperModel

# "small" is a reasonable CPU-only accuracy/speed tradeoff for Hebrew.
# Swap to "medium" if this container gets relocated to a beefier machine.
_stt_model = WhisperModel("small", device="cpu", compute_type="int8")


def transcribe(pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
    audio = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = _stt_model.transcribe(audio, language="he")
    return "".join(seg.text for seg in segments).strip()


def synthesize(text: str) -> bytes:
    """
    TODO: wire up Piper (local, free) or ElevenLabs (cloud, higher quality)
    here. Returning silence for now so the pipeline is wireable/testable
    end-to-end before a TTS engine is chosen.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)  # 0.1s of silence placeholder
    return buf.getvalue()


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
