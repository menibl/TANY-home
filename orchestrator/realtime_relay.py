"""
Server-to-server OpenAI Realtime relay — powers LLM_PROVIDER=openai.

capture-svc's /session WebSocket contract (see shared/API_CONTRACTS.md) is
transport-agnostic: raw PCM16 frames flow up, JSON control messages or raw
PCM bytes flow down. capture-svc doesn't know or care which brain is on
the other end, so this module can swap in for the Claude cascaded loop in
main.py without capture-svc changing at all — it just needs to run
wherever its mic/speaker are (the Pi, the PC, or both on one machine).

This opens its OWN WebSocket directly to OpenAI's Realtime API
(server-to-server, no browser, no WebRTC — see
https://platform.openai.com/docs/guides/realtime-websocket) and pumps
audio both ways instead of doing STT -> Claude -> TTS locally. That is
what lets a plain capture-svc <-> orchestrator audio pipe reuse OpenAI's
native speech-to-speech without a browser sitting on either machine.

Distinct from realtime.py, which is the *browser* WebRTC integration used
by capture-svc's dashboard (USE_REALTIME=1) — audio there flows directly
between the browser tab and OpenAI and never touches this backend. This
module exists for the opposite case: no browser anywhere, just capture-svc's
own mic/speaker piped through this backend as a relay.

NOTE: OpenAI's Realtime event names/fields have moved before and may have
moved again since this was written — verify against the current docs
above when first testing against a real API key, in particular the audio
delta event name (handled here as both "response.audio.delta" and
"response.output_audio.delta" to hedge against that).
"""
import asyncio
import base64
import hashlib
import json
import logging
import os

import httpx
import websockets
from fastapi import WebSocket, WebSocketDisconnect

from skills import Skill
from speech_io import SilenceBasedEndpointer

log = logging.getLogger("orchestrator.realtime_relay")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
REALTIME_MODEL = os.environ.get("REALTIME_MODEL", "gpt-realtime")
REALTIME_VOICE = os.environ.get("REALTIME_VOICE", "marin")
VOICE_SVC_URL = os.environ.get("VOICE_SVC_URL", "http://voice-id-svc:8002")
REALTIME_WS_URL = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"

# Mirrors realtime.py's client-side-only tool — here the relay itself
# (not a browser) intercepts it and ends the session after the farewell
# turn finishes playing.
END_CONVERSATION_TOOL = {
    "type": "function",
    "name": "end_conversation",
    "description": "קורא לפונקציה הזו כשהמשתמש מסמן שהשיחה נגמרה (למשל 'תודה, סיימתי', 'זהו תודה', 'להתראות'). אמור קודם משפט סיום קצר וחם, ואז קרא לפונקציה.",
    "parameters": {"type": "object", "properties": {}},
}


def _to_realtime_tools(claude_style_tools: list[dict]) -> list[dict]:
    """Claude's tool schema uses input_schema; OpenAI Realtime uses a flat
    parameters field (no nested "function" wrapper)."""
    return [
        {"type": "function", "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}
        for t in claude_style_tools
    ]


async def _confirm_identity(utterance_pcm16: bytes) -> dict | None:
    """Same stage-3 fallback as main.py's Claude path: if the session
    started uncertain who this is, check voice-id-svc once real speech
    has accumulated. Runs off the audio the relay is already forwarding —
    it doesn't touch what OpenAI sees, it's just a side tap."""
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
        log.exception("voice-id confirmation call failed during realtime relay")
        return None


async def _handle_function_call(item: dict, oai_ws, client_ws: WebSocket, registry: dict[str, Skill], user_id):
    name = item.get("name")
    call_id = item.get("call_id")
    if name == "end_conversation":
        return

    try:
        args = json.loads(item.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}

    await client_ws.send_text(json.dumps({"type": "tool_call", "name": name, "status": "running"}))
    skill = registry.get(name)
    if not skill:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    else:
        try:
            result = await skill.handler(args, user_id)
        except Exception as e:
            log.exception("realtime relay tool call %s failed", name)
            result = {"ok": False, "error": str(e)}

    await oai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result, ensure_ascii=False),
        },
    }))
    await oai_ws.send(json.dumps({"type": "response.create"}))


async def run(client_ws: WebSocket, *, user_id, certain, system_prompt, tools, registry: dict[str, Skill]):
    if not OPENAI_API_KEY:
        await client_ws.close(code=1011, reason="OPENAI_API_KEY not configured")
        return

    realtime_tools = _to_realtime_tools(tools)
    realtime_tools.append(END_CONVERSATION_TOOL)

    # Folded into the session's own first turn (not a separate TTS call)
    # so there is one continuous voice from "שלום" onward — same reasoning
    # as realtime.py's browser path.
    greeting_name = user_id if certain else None
    greeting_instruction = (
        f'פתח את השיחה מיד באמירת "שלום {greeting_name}" ותו לא, ואז המתן שהמשתמש ידבר.'
        if greeting_name else
        'פתח את השיחה מיד באמירת "שלום" ותו לא, ואז המתן שהמשתמש ידבר.'
    )
    end_instruction = (
        'כשהמשתמש אומר משהו שמסמן שהשיחה נגמרה (למשל "תודה, סיימתי", "זהו תודה", "להתראות") — '
        "אמור משפט סיום קצר וחם ואז קרא לפונקציה end_conversation."
    )
    instructions = f"{system_prompt}\n\n{greeting_instruction}\n{end_instruction}"

    safety_id = "homebot-" + hashlib.sha256((user_id or "guest").encode("utf-8")).hexdigest()[:16]

    # websockets==12.0's connect() takes extra_headers (renamed to
    # additional_headers in later versions) — check the pin in
    # requirements.txt before touching this if it's ever upgraded.
    async with websockets.connect(
        REALTIME_WS_URL,
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Safety-Identifier": safety_id,
        },
        max_size=None,
    ) as oai_ws:
        log.info("DEBUG: connected to OpenAI realtime WS")
        await oai_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "audio": {
                    "input": {"turn_detection": {"type": "server_vad", "threshold": 0.6, "silence_duration_ms": 600}},
                    "output": {"voice": REALTIME_VOICE},
                },
                "tools": realtime_tools,
            },
        }))
        await oai_ws.send(json.dumps({"type": "response.create"}))
        log.info("DEBUG: sent session.update + response.create")

        # Only used to segment utterances for the voice-id side-check
        # below, never for transcription/reply generation — OpenAI does
        # its own turn detection server-side on the audio we forward it.
        endpointer = SilenceBasedEndpointer()
        state = {"user_id": user_id, "certain": certain}

        async def pump_mic_up():
            log.info("DEBUG: pump_mic_up starting")
            frame_count = 0
            try:
                while True:
                    frame = await client_ws.receive_bytes()
                    frame_count += 1
                    if frame_count % 100 == 0:
                        log.info("DEBUG: pump_mic_up forwarded %d frames", frame_count)
                    if not state["certain"]:
                        utterance = endpointer.push(frame)
                        if utterance is not None:
                            confirmed = await _confirm_identity(utterance)
                            if confirmed:
                                state["user_id"] = confirmed["best_guess"]
                                state["certain"] = True
                                await client_ws.send_text(json.dumps({
                                    "type": "identity_update",
                                    "user_id": state["user_id"],
                                    "confidence": confirmed["confidence"],
                                }))
                    await oai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(frame).decode(),
                    }))
            except WebSocketDisconnect:
                pass

        async def pump_events_down():
            # capture-svc's play_pcm16 opens a fresh output stream per
            # binary message it receives (fine for the Claude path, which
            # sends one message per complete reply) — forwarding each
            # small OpenAI audio delta (dozens per turn) as its own
            # message meant dozens of open/close cycles per reply, which
            # on a Bluetooth SCO link (real per-open latency) is what
            # made playback sound fragmented/choppy instead of like
            # continuous speech. Buffer deltas and flush one combined
            # blob per output item instead.
            audio_buffer = bytearray()

            async for raw in oai_ws:
                event = json.loads(raw)
                etype = event.get("type")
                log.info("DEBUG: OpenAI event: %s", etype)

                if etype in ("response.audio.delta", "response.output_audio.delta"):
                    audio_buffer.extend(base64.b64decode(event["delta"]))

                elif etype in ("response.audio.done", "response.output_audio.done"):
                    if audio_buffer:
                        await client_ws.send_bytes(bytes(audio_buffer))
                        audio_buffer.clear()

                elif etype == "response.done":
                    if audio_buffer:
                        await client_ws.send_bytes(bytes(audio_buffer))
                        audio_buffer.clear()
                    await client_ws.send_text(json.dumps({"type": "end_of_turn"}))
                    output = (event.get("response") or {}).get("output", [])
                    for item in output:
                        if item.get("type") == "function_call":
                            await _handle_function_call(item, oai_ws, client_ws, registry, state["user_id"])
                            if item.get("name") == "end_conversation":
                                await client_ws.send_text(json.dumps({"type": "session_end"}))
                                return

                elif etype == "error":
                    log.error("OpenAI realtime error: %s", event)

        mic_task = asyncio.create_task(pump_mic_up())
        events_task = asyncio.create_task(pump_events_down())
        done, pending = await asyncio.wait({mic_task, events_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
