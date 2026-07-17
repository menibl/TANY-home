import asyncio
import json
import logging
import os

import httpx
import redis
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from llm_adapters import build_engine
from personality import load_base_personality, load_user_profile, build_system_prompt
from skills import build_registry, enabled_tools_for
from speech_io import transcribe, synthesize, TTS_SAMPLE_RATE, SilenceBasedEndpointer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [orchestrator] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="homebot-orchestrator")

PROFILE_STORE_URL = os.environ.get("PROFILE_STORE_URL", "redis://profile-store:6379")
TANY_BRIDGE_URL = os.environ.get("TANY_BRIDGE_URL", "http://tany-bridge:8005")
VOICE_SVC_URL = os.environ.get("VOICE_SVC_URL", "http://voice-id-svc:8002")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "claude")

r = redis.from_url(PROFILE_STORE_URL)
registry = build_registry(TANY_BRIDGE_URL)
engine = build_engine(LLM_PROVIDER)


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


@app.websocket("/session")
async def session(ws: WebSocket):
    await ws.accept()
    user_id = ws.query_params.get("user_id") or None
    certain = ws.query_params.get("certain") == "true"

    base_personality = load_base_personality(r)
    user_profile = load_user_profile(r, user_id)
    system_prompt = build_system_prompt(base_personality, user_profile, user_id)
    tools = enabled_tools_for(registry, user_profile["skills_enabled"])

    endpointer = SilenceBasedEndpointer()
    history: list[dict] = []

    log.info("session started: user_id=%s certain=%s endpoint_threshold=%.3f silence_ms=%d",
              user_id, certain, endpointer.energy_threshold, endpointer.silence_frames_needed * 20)

    try:
        while True:
            frame = await ws.receive_bytes()
            utterance = endpointer.push(frame)
            if utterance is None:
                continue

            # Stage 3 fallback: still unidentified -> try to confirm now that
            # we have real speech, and update the system prompt if it lands.
            if not certain:
                confirmed = await confirm_identity_from_audio(utterance)
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
            text = await asyncio.to_thread(transcribe, utterance)
            if not text:
                continue
            await ws.send_text(json.dumps({"type": "partial_transcript", "text": text}))

            reply = await engine.respond(system_prompt, text, tools, history)
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
            audio = await asyncio.to_thread(synthesize, reply_text)
            await ws.send_bytes(audio)
            await ws.send_text(json.dumps({"type": "end_of_turn"}))

    except WebSocketDisconnect:
        log.info("session ended: user_id=%s", user_id)
