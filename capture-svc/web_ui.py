"""
Local web dashboard for capture-svc: live status (replaces the old
Tkinter status_gui) + a "new user" enrollment flow (face photo, body
photo, voice sample) that calls vision-id-svc's and voice-id-svc's
existing /enroll endpoints.

Kept separate from main.py and wired up via configure() to avoid a
circular import (main.py needs to run this app; this app needs main.py's
mic/camera helpers).
"""
import asyncio
import base64
import json
import logging
import time
from pathlib import Path

import numpy as np
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

log = logging.getLogger("capture-svc.web_ui")
app = FastAPI(title="homebot-capture-ui")

STATIC_DIR = Path(__file__).parent / "static"

# Filled in by configure() — avoids importing main.py at module load time
_cfg = {}


def configure(*, rtsp_url, vision_svc_url, voice_svc_url, open_mic_stream,
              grab_snapshots, sample_rate, mic_yield_requested, mic_yielded, mic_resume,
              manual_trigger_event, end_session_event):
    _cfg.update(
        rtsp_url=rtsp_url, vision_svc_url=vision_svc_url, voice_svc_url=voice_svc_url,
        open_mic_stream=open_mic_stream, grab_snapshots=grab_snapshots, sample_rate=sample_rate,
        mic_yield_requested=mic_yield_requested, mic_yielded=mic_yielded, mic_resume=mic_resume,
        manual_trigger_event=manual_trigger_event, end_session_event=end_session_event,
    )


@app.post("/api/trigger")
async def manual_trigger():
    """The dashboard's "click the orb" button — fires the exact same
    flow as a real double-clap, except identification uses the local
    webcam (see main.py's manual_trigger_event) since clicking obviously
    means you're at the computer, not wherever the RTSP camera points."""
    _cfg["manual_trigger_event"].set()
    return {"ok": True}


@app.get("/api/state")
async def state_plain():
    """Plain-text current state (just the word, e.g. "listening" or
    "session") for clients too small to bother parsing JSON — the
    M5StickC firmware polls this to know whether a conversation is
    active (anything other than "listening", the idle/waiting-for-
    trigger state) so it can animate its eyes accordingly."""
    return PlainTextResponse(_last_state["state"])


@app.post("/api/end-session")
async def end_session():
    """The dashboard's "סיים שיחה" button — ends the active conversation
    on demand instead of waiting for the orchestrator to. A no-op if
    there's no session open."""
    _cfg["end_session_event"].set()
    return {"ok": True}


# ---------------------------------------------------------------- status ws
class _Broadcaster:
    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def register(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)

    def unregister(self, ws: WebSocket):
        self._clients.discard(ws)

    async def send(self, payload: dict):
        data = json.dumps(payload)
        dead = []
        for ws in self._clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


broadcaster = _Broadcaster()
_last_state = {"state": "listening", "text": ""}


def broadcast_status(state: str, text: str = "", **extra):
    """Thread/coroutine-agnostic entry point called from main.py's set_status().
    Schedules the actual send onto whatever loop is running this app.
    `extra` is for payloads beyond state/text — e.g. realtime mode passes
    user_id/certain so the browser knows who it's opening a WebRTC session
    for without a separate round-trip."""
    payload = {"state": state, "text": text, **extra}
    # "realtime_start" is a one-shot command telling the browser to open a
    # new WebRTC session, not a steady state — it stays the broadcast
    # state for the whole conversation (nothing else gets set until it
    # ends). If it were replayed here, any page reload or WS reconnect
    # during (or right after) a live conversation would silently launch
    # another session with no clap/click involved. Only durable states
    # get remembered for replay; one-shot triggers are sent live only.
    if state != "realtime_start":
        _last_state.clear()
        _last_state.update(payload)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(broadcaster.send(payload))
    except RuntimeError:
        pass  # no loop yet (startup) — the client fetches current state on connect


@app.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    await broadcaster.register(ws)
    await ws.send_text(json.dumps(_last_state))
    try:
        while True:
            await ws.receive_text()  # client sends nothing; just keep the socket open
    except WebSocketDisconnect:
        broadcaster.unregister(ws)


# ------------------------------------------------------------------ pages
@app.get("/")
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/enroll")
async def enroll_page():
    return FileResponse(STATIC_DIR / "enroll.html")


# ------------------------------------------------------------ enrollment
async def _yield_mic():
    """Ask main_loop's clap-listener to release the mic, and wait for it
    to confirm. Both sides use open_mic_stream() on the same physical
    device — holding it open in two places at once was the cause of the
    near-silent-audio bug fixed earlier, so enrollment recording must
    wait its turn instead of opening a second stream concurrently."""
    _cfg["mic_yield_requested"].set()
    await _cfg["mic_yielded"].wait()


def _resume_mic():
    _cfg["mic_yield_requested"].clear()
    _cfg["mic_resume"].set()


@app.post("/api/enroll/capture-face")
async def capture_face():
    try:
        frames = await asyncio.to_thread(_cfg["grab_snapshots"], _cfg["rtsp_url"], 1)
        _recording_state["last_face_b64"] = frames[0]
        return {"ok": True, "image_b64": frames[0]}
    except Exception as e:
        log.exception("face capture failed")
        return {"ok": False, "error": str(e)}


@app.post("/api/enroll/capture-body")
async def capture_body():
    try:
        frames = await asyncio.to_thread(_cfg["grab_snapshots"], _cfg["rtsp_url"], 1)
        return {"ok": True, "image_b64": frames[0]}
    except Exception as e:
        log.exception("body capture failed")
        return {"ok": False, "error": str(e)}


_recording_state = {"active": False, "chunks": None, "stream": None}


@app.post("/api/enroll/voice/start")
async def voice_start():
    if _recording_state["active"]:
        return {"ok": False, "error": "already recording"}
    await _yield_mic()
    chunks: list[bytes] = []

    def on_frame(frame: np.ndarray):
        chunks.append(frame.tobytes())

    stream = _cfg["open_mic_stream"](on_frame)
    stream.start()
    _recording_state.update(active=True, chunks=chunks, stream=stream, started_at=time.monotonic())
    return {"ok": True}


@app.post("/api/enroll/voice/stop")
async def voice_stop():
    if not _recording_state["active"]:
        return {"ok": False, "error": "not recording"}
    stream = _recording_state["stream"]
    stream.stop()
    stream.close()
    chunks = _recording_state["chunks"]
    duration_s = time.monotonic() - _recording_state["started_at"]
    _recording_state.update(active=False, chunks=None, stream=None)
    _resume_mic()

    if not chunks or duration_s < 0.5:
        return {"ok": False, "error": "הקלטה קצרה מדי, נסה שוב"}

    pcm = b"".join(chunks)
    _recording_state["last_pcm_b64"] = base64.b64encode(pcm).decode()
    return {"ok": True, "duration_s": duration_s}


class SubmitRequest(BaseModel):
    user_id: str


@app.post("/api/enroll/submit")
async def submit_enrollment(req: SubmitRequest):
    pcm_b64 = _recording_state.get("last_pcm_b64")
    if not pcm_b64:
        return {"ok": False, "error": "אין הקלטת קול — הקלט לפני שמירה"}

    try:
        face_resp = await asyncio.to_thread(
            requests.post, f"{_cfg['vision_svc_url']}/enroll",
            json={"user_id": req.user_id, "image_b64": _recording_state.get("last_face_b64", "")},
            timeout=10,
        )
        voice_resp = await asyncio.to_thread(
            requests.post, f"{_cfg['voice_svc_url']}/enroll",
            json={"user_id": req.user_id, "audio_b64": pcm_b64, "sample_rate": _cfg["sample_rate"]},
            timeout=15,
        )
        face_data, voice_data = face_resp.json(), voice_resp.json()
        if not face_data.get("ok") or not voice_data.get("ok"):
            return {"ok": False, "error": face_data.get("error") or voice_data.get("error") or "שגיאה לא ידועה"}
        return {"ok": True, "face": face_data, "voice": voice_data}
    except Exception as e:
        log.exception("enrollment submit failed")
        return {"ok": False, "error": str(e)}


@app.get("/api/enroll/users")
async def list_enrolled_users():
    """Merges vision-id-svc's and voice-id-svc's per-user sample counts
    into one list keyed by user_id, so the enrollment page can show who's
    registered instead of enrolling blind — this was the only way to
    notice a user was stuck on one bad low-confidence sample."""
    try:
        face_resp, voice_resp = await asyncio.gather(
            asyncio.to_thread(requests.get, f"{_cfg['vision_svc_url']}/users", timeout=5),
            asyncio.to_thread(requests.get, f"{_cfg['voice_svc_url']}/users", timeout=5),
        )
        face_counts = {u["user_id"]: u["samples"] for u in face_resp.json().get("users", [])}
        voice_counts = {u["user_id"]: u["samples"] for u in voice_resp.json().get("users", [])}
        all_ids = sorted(set(face_counts) | set(voice_counts))
        return {"ok": True, "users": [
            {"user_id": uid, "face_samples": face_counts.get(uid, 0), "voice_samples": voice_counts.get(uid, 0)}
            for uid in all_ids
        ]}
    except Exception as e:
        log.exception("listing enrolled users failed")
        return {"ok": False, "error": str(e), "users": []}


@app.delete("/api/enroll/users/{user_id}")
async def delete_enrolled_user(user_id: str):
    """Clears both the face and voice samples for a user so they can be
    re-enrolled from scratch instead of just piling more samples on top
    of old ones (enroll only ever appends)."""
    try:
        await asyncio.gather(
            asyncio.to_thread(requests.delete, f"{_cfg['vision_svc_url']}/users/{user_id}", timeout=5),
            asyncio.to_thread(requests.delete, f"{_cfg['voice_svc_url']}/users/{user_id}", timeout=5),
        )
        return {"ok": True}
    except Exception as e:
        log.exception("deleting enrolled user failed")
        return {"ok": False, "error": str(e)}
