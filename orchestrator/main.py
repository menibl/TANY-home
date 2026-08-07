import asyncio
import json
import logging
import os
import time

import httpx
import redis
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import realtime_relay
from llm_adapters import build_engine, OpenAIRealtimeEngine
from personality import load_base_personality, load_user_profile, build_system_prompt, save_user_profile_fields
from skills import build_registry, enabled_tools_for
from speech_io import transcribe, synthesize, TTS_SAMPLE_RATE, SilenceBasedEndpointer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [orchestrator] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="homebot-orchestrator")

# The dashboard (capture-svc's web_ui, a different origin/port) calls
# /realtime/session and /realtime/tool-call directly from the browser —
# both are local-only endpoints on a home LAN, so a wide-open CORS policy
# here isn't the risk it'd be on a public deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSION_IDLE_TIMEOUT_S = float(os.environ.get("SESSION_IDLE_TIMEOUT_S", "20"))

PROFILE_STORE_URL = os.environ.get("PROFILE_STORE_URL", "redis://profile-store:6379")
TANY_BRIDGE_URL = os.environ.get("TANY_BRIDGE_URL", "http://tany-bridge:8005")
VOICE_SVC_URL = os.environ.get("VOICE_SVC_URL", "http://voice-id-svc:8002")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "claude")

r = redis.from_url(PROFILE_STORE_URL)
registry = build_registry(TANY_BRIDGE_URL, r)
engine = build_engine(LLM_PROVIDER)

import realtime as realtime_mod
realtime_mod.register(app, r=r, registry=registry, tany_bridge_url=TANY_BRIDGE_URL)


async def confirm_identity_from_audio(utterance_pcm16: bytes) -> dict | None:
    """Stage 3 fallback: if we entered the session uncertain who this is,
    ask voice-id-svc once we have a real utterance to check against."""
    import base64
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{VOICE_SVC_URL}/identify",
                json={"audio_b64": base64.b64encode(utterance_pcm16).decode(), "sample_rate": 16000},
            )
            resp.raise_for_status()
            result = resp.json()
            return result if result.get("certain") else None
    except Exception:
        log.exception("voice-id confirmation call failed, continuing as unidentified")
        return None


async def run_tool_calls(tool_calls: list[dict], user_id: str | None) -> list[dict]:
    results = []
    for call in tool_calls:
        skill = registry.get(call["name"])
        if not skill:
            results.append({"tool_use_id": call["id"], "content": "סקיל לא ידוע"})
            continue
        try:
            outcome = await skill.handler(call["input"], user_id)
            results.append({"tool_use_id": call["id"], "content": json.dumps(outcome, ensure_ascii=False)})
        except Exception as e:
            log.exception("skill %s failed", call["name"])
            results.append({"tool_use_id": call["id"], "content": f"שגיאה: {e}"})
    return results


class TanyTokenRequest(BaseModel):
    token: str


@app.post("/profile/{user_id}/tany-token")
async def set_tany_token(user_id: str, req: TanyTokenRequest):
    """Called from /enroll — each person gets their own token from TANY
    and enters it once here so skills.py's tany_command skill can use it
    on their behalf."""
    save_user_profile_fields(r, user_id, tany_token=req.token)
    return {"ok": True}


class TTSRequest(BaseModel):
    text: str


@app.post("/tts")
async def tts(req: TTSRequest):
    """Used by capture-svc for the one-off greeting before a conversation
    session opens (the in-session replies are synthesized and streamed
    over the /session websocket instead)."""
    audio = await asyncio.to_thread(synthesize, req.text)
    return Response(
        content=audio,
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": str(TTS_SAMPLE_RATE)},
    )


async def _next_utterance(ws: WebSocket, endpointer: SilenceBasedEndpointer, timeout_s: float) -> bytes | None:
    """Waits for the next complete utterance, giving up (returning None)
    if `timeout_s` passes with no speech — capture-svc's mic stream keeps
    sending raw frames continuously (background noise included) the whole
    time it's open, so a plain per-frame timeout would never fire; this
    tracks a single deadline across as many frames as it takes."""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            frame = await asyncio.wait_for(ws.receive_bytes(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        utterance = endpointer.push(frame)
        if utterance is not None:
            return utterance


@app.websocket("/session")
async def session(ws: WebSocket):
    await ws.accept()
    user_id = ws.query_params.get("user_id") or None
    certain = ws.query_params.get("certain") == "true"

    base_personality = load_base_personality(r)
    user_profile = load_user_profile(r, user_id)
    system_prompt = build_system_prompt(base_personality, user_profile, user_id)
    tools = enabled_tools_for(registry, user_profile["skills_enabled"])

    if isinstance(engine, OpenAIRealtimeEngine):
        # LLM_PROVIDER=openai: native speech-to-speech, bypass STT/TTS
        # entirely and relay raw audio to OpenAI's own Realtime session
        # instead of running the cascaded loop below. capture-svc sees
        # the exact same WS contract either way.
        log.info("session started (openai realtime relay): user_id=%s certain=%s", user_id, certain)
        try:
            await realtime_relay.run(
                ws, user_id=user_id, certain=certain,
                system_prompt=system_prompt, tools=tools, registry=registry,
            )
        except WebSocketDisconnect:
            pass
        log.info("session ended (openai realtime relay): user_id=%s", user_id)
        return

    endpointer = SilenceBasedEndpointer()
    history: list[dict] = []

    log.info("session started: user_id=%s certain=%s endpoint_threshold=%.3f silence_ms=%d",
              user_id, certain, endpointer.energy_threshold, endpointer.silence_frames_needed * 20)

    try:
        while True:
            utterance = await _next_utterance(ws, endpointer, SESSION_IDLE_TIMEOUT_S)
            if utterance is None:
                log.info("session auto-ended (idle %.0fs): user_id=%s", SESSION_IDLE_TIMEOUT_S, user_id)
                await ws.send_text(json.dumps({"type": "session_end", "reason": "idle_timeout"}))
                return

            # Stage 3 fallback: still unidentified -> try to confirm now that
            # we have real speech, and update the system prompt if it lands.
            if not certain:
                t0 = time.monotonic()
                confirmed = await confirm_identity_from_audio(utterance)
                log.info("TIMING voice-id confirm: %.2fs", time.monotonic() - t0)
                if confirmed:
                    user_id = confirmed["best_guess"]
                    certain = True
                    user_profile = load_user_profile(r, user_id)
                    system_prompt = build_system_prompt(base_personality, user_profile, user_id)
                    tools = enabled_tools_for(registry, user_profile["skills_enabled"])
                    await ws.send_text(json.dumps({
                        "type": "identity_update",
                        "user_id": user_id,
                        "confidence": confirmed["confidence"],
                    }))

            # transcribe/synthesize are blocking (CPU-bound whisper, sync
            # OpenAI TTS call) — run off the event loop so ping/pong
            # keepalive frames keep flowing on slow turns and the client
            # doesn't time out and disconnect mid-response
            t0 = time.monotonic()
            text = await asyncio.to_thread(transcribe, utterance)
            log.info("TIMING whisper transcribe: %.2fs -> %r", time.monotonic() - t0, text)
            if not text:
                continue
            await ws.send_text(json.dumps({"type": "partial_transcript", "text": text}))

            t0 = time.monotonic()
            reply = await engine.respond(system_prompt, text, tools, history)
            log.info("TIMING claude respond: %.2fs", time.monotonic() - t0)
            history.append({"role": "user", "content": text})

            if reply["tool_calls"]:
                for call in reply["tool_calls"]:
                    await ws.send_text(json.dumps({"type": "tool_call", "name": call["name"], "status": "running"}))
                tool_results = await run_tool_calls(reply["tool_calls"], user_id)
                # feed tool results back to the model for a natural-language follow-up
                history.append({"role": "assistant", "content": reply["text"] or ""})
                follow_up = await engine.respond(
                    system_prompt,
                    "\n".join(tr["content"] for tr in tool_results),
                    tools,
                    history,
                )
                reply_text = follow_up["text"]
            else:
                reply_text = reply["text"]

            history.append({"role": "assistant", "content": reply_text})
            t0 = time.monotonic()
            audio = await asyncio.to_thread(synthesize, reply_text)
            log.info("TIMING tts synthesize: %.2fs", time.monotonic() - t0)
            await ws.send_bytes(audio)
            await ws.send_text(json.dumps({"type": "end_of_turn"}))

    except WebSocketDisconnect:
        log.info("session ended: user_id=%s", user_id)
