import base64
import json
import os

import numpy as np
import redis
from fastapi import FastAPI
from pydantic import BaseModel
from resemblyzer import VoiceEncoder, preprocess_wav

app = FastAPI(title="voice-id-svc")

r = redis.from_url(os.environ.get("PROFILE_STORE_URL", "redis://profile-store:6379"))
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.75"))

# Loaded once at startup — this is the one moderately heavy object in this
# service (~17MB weights), fine on CPU, not something to reload per-request.
encoder = VoiceEncoder()


class IdentifyRequest(BaseModel):
    audio_b64: str
    sample_rate: int = 16000


class EnrollRequest(BaseModel):
    user_id: str
    audio_b64: str
    sample_rate: int = 16000


def _embed(audio_b64: str, sample_rate: int) -> np.ndarray:
    # audio_b64 is raw PCM16 (no WAV header) — decode straight to a
    # float32 waveform instead of handing the bytes to preprocess_wav,
    # which expects a path/WAV-file/ndarray and can't parse headerless PCM
    raw = base64.b64decode(audio_b64)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    wav = preprocess_wav(pcm, source_sr=sample_rate)
    return encoder.embed_utterance(wav)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _known_users() -> list[str]:
    return [k.decode().split(":")[1] for k in r.keys("user:*:voice_embeddings")]


@app.post("/identify")
def identify(req: IdentifyRequest):
    query_emb = _embed(req.audio_b64, req.sample_rate)

    candidates = []
    for user_id in _known_users():
        stored = json.loads(r.get(f"user:{user_id}:voice_embeddings"))
        best = max(_cosine(query_emb, np.array(v)) for v in stored)
        candidates.append({"user_id": user_id, "confidence": round(best, 3)})

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    if not candidates:
        return {"candidates": [], "best_guess": None, "confidence": 0.0, "certain": False}

    best = candidates[0]
    return {
        "candidates": candidates,
        "best_guess": best["user_id"],
        "confidence": best["confidence"],
        "certain": best["confidence"] >= MATCH_THRESHOLD,
    }


@app.post("/enroll")
def enroll(req: EnrollRequest):
    emb = _embed(req.audio_b64, req.sample_rate)
    key = f"user:{req.user_id}:voice_embeddings"
    existing = json.loads(r.get(key)) if r.get(key) else []
    existing.append(emb.tolist())
    r.set(key, json.dumps(existing))
    return {"ok": True, "samples_stored": len(existing)}
