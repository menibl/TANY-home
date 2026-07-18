import base64
import io
import json
import os

import face_recognition
import numpy as np
import redis
from fastapi import FastAPI
from PIL import Image
from pydantic import BaseModel

app = FastAPI(title="vision-id-svc")

r = redis.from_url(os.environ.get("PROFILE_STORE_URL", "redis://profile-store:6379"))
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.55"))

# Fusion weights. If a signal is unavailable for a given frame (e.g. no
# face detected because of the camera angle), its weight is redistributed
# across whatever signals ARE available rather than counted as zero.
BASE_WEIGHTS = {"face": 0.6, "body": 0.25, "hair": 0.15}


class IdentifyRequest(BaseModel):
    image_b64: str
    captured_at: str | None = None


def _decode_image(image_b64: str) -> np.ndarray:
    raw = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    return np.array(img)


def _face_embedding(image: np.ndarray) -> np.ndarray | None:
    locations = face_recognition.face_locations(image)
    if not locations:
        return None
    encodings = face_recognition.face_encodings(image, known_face_locations=locations)
    return encodings[0] if encodings else None


def _soft_biometrics(image: np.ndarray) -> dict:
    """
    Placeholder for the non-face signals (body build via pose estimation,
    hair color/length via a small classifier). Kept as its own function so
    a real MediaPipe-Pose / hair-color model can be dropped in later
    without touching the fusion logic below.
    """
    # TODO: replace with a real MediaPipe Pose + hair-color classifier
    return {"body_features": None, "hair_features": None}


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    a, b = np.asarray(a), np.asarray(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _known_users() -> list[str]:
    return [k.decode().split(":")[1] for k in r.keys("user:*:face_embeddings")]


def _score_against_profile(user_id: str, face_emb, soft_feats: dict) -> dict:
    signals = {}

    raw = r.get(f"user:{user_id}:face_embeddings")
    if raw and face_emb is not None:
        stored = json.loads(raw)
        # face_recognition uses L2 distance; convert to a 0..1 similarity-ish score
        dists = [np.linalg.norm(face_emb - np.array(v)) for v in stored]
        best_dist = min(dists) if dists else 1.0
        signals["face"] = max(0.0, 1.0 - best_dist / 0.6)  # 0.6 ~= typical match threshold

    # TODO: once _soft_biometrics is implemented, score body/hair here too
    # against stored profile vectors the same way.

    if not signals:
        return {"user_id": user_id, "confidence": 0.0, "signals": signals}

    active_weight = sum(BASE_WEIGHTS[k] for k in signals)
    confidence = sum(BASE_WEIGHTS[k] * v for k, v in signals.items()) / active_weight
    return {"user_id": user_id, "confidence": round(float(confidence), 3), "signals": signals}


@app.post("/identify")
def identify(req: IdentifyRequest):
    image = _decode_image(req.image_b64)
    face_emb = _face_embedding(image)
    soft_feats = _soft_biometrics(image)

    candidates = [
        _score_against_profile(user_id, face_emb, soft_feats)
        for user_id in _known_users()
    ]
    candidates.sort(key=lambda c: c["confidence"], reverse=True)

    if not candidates:
        return {"candidates": [], "best_guess": None, "confidence": 0.0, "certain": False}

    best = candidates[0]
    return {
        "candidates": candidates,
        "best_guess": best["user_id"],
        "confidence": float(best["confidence"]),
        # numpy.float64 >= float yields numpy.bool_, which FastAPI's
        # response serialization chokes on ("not iterable") — every real
        # /identify call was 500ing on this, silently falling back to an
        # unidentified generic greeting even for an enrolled face
        "certain": bool(best["confidence"] >= MATCH_THRESHOLD),
    }


class EnrollRequest(BaseModel):
    user_id: str
    image_b64: str


@app.post("/enroll")
def enroll(req: EnrollRequest):
    image = _decode_image(req.image_b64)
    emb = _face_embedding(image)
    if emb is None:
        return {"ok": False, "error": "no face detected in enrollment image"}

    key = f"user:{req.user_id}:face_embeddings"
    existing = json.loads(r.get(key)) if r.get(key) else []
    existing.append(emb.tolist())
    r.set(key, json.dumps(existing))
    return {"ok": True, "samples_stored": len(existing)}
