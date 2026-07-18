"""
OpenAI Realtime integration (WebRTC, browser <-> OpenAI directly).

This is a genuinely different shape from the Claude path in main.py:
Claude has no native speech-to-speech API, so that path is a cascaded
pipeline (capture-svc mic -> orchestrator STT -> Claude -> orchestrator
TTS -> capture-svc speaker), all audio round-tripping through Python.
OpenAI Realtime *is* speech-to-speech, and over WebRTC the audio flows
directly between the browser (the dashboard page) and OpenAI — this
backend's only job is:
  1. broker the SDP handshake (inject the API key server-side, so it
     never reaches the browser; also where the per-user system prompt
     and tool schemas get attached)
  2. execute tool/function calls the model requests (the data channel
     tells the browser a tool was called; the browser posts it here;
     this reuses the exact same skills.py registry as the Claude path)

Not used by the default LLM_PROVIDER=claude flow at all — this only
activates for the dashboard's realtime mode.
"""
import hashlib
import json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request

from personality import build_system_prompt, load_base_personality, load_user_profile
from skills import Skill, enabled_tools_for

log = logging.getLogger("orchestrator.realtime")
router = APIRouter()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
REALTIME_MODEL = os.environ.get("REALTIME_MODEL", "gpt-realtime")
REALTIME_VOICE = os.environ.get("REALTIME_VOICE", "marin")


def _to_realtime_tools(claude_style_tools: list[dict]) -> list[dict]:
    """Claude's tool schema uses input_schema; OpenAI Realtime uses
    parameters, and each entry is flat (no nested "function" wrapper —
    that's the older Chat Completions shape, not Realtime's)."""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }
        for t in claude_style_tools
    ]


def register(app, *, r, registry: dict[str, Skill], tany_bridge_url: str):
    """Wires the realtime routes into the FastAPI app. Takes the same
    redis handle + skill registry main.py already built so both paths
    (Claude cascaded, OpenAI Realtime) share one source of truth for
    personality and tools instead of drifting apart."""

    @router.post("/realtime/session")
    async def create_realtime_session(request: Request):
        if not OPENAI_API_KEY:
            raise HTTPException(500, "OPENAI_API_KEY not configured")

        user_id = request.query_params.get("user_id") or None
        certain = request.query_params.get("certain") == "true"
        sdp_offer = (await request.body()).decode("utf-8")

        base_personality = load_base_personality(r)
        user_profile = load_user_profile(r, user_id if certain else None)
        system_prompt = build_system_prompt(base_personality, user_profile, user_id if certain else None)
        tools = _to_realtime_tools(enabled_tools_for(registry, user_profile["skills_enabled"]))

        session_config = {
            "type": "realtime",
            "model": REALTIME_MODEL,
            "instructions": system_prompt,
            "audio": {"output": {"voice": REALTIME_VOICE}},
            "tools": tools,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/realtime/calls",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    # HTTP headers are ASCII-only — user_id is Hebrew
                    # ("מני"), which crashed here with UnicodeEncodeError.
                    # This header only needs to be a stable per-user
                    # identifier for OpenAI's abuse monitoring, not
                    # human-readable, so hash it instead of encoding it.
                    "OpenAI-Safety-Identifier": "homebot-" + hashlib.sha256(
                        (user_id or "guest").encode("utf-8")
                    ).hexdigest()[:16],
                },
                files={
                    "sdp": (None, sdp_offer),
                    "session": (None, json.dumps(session_config), "application/json"),
                },
            )

        if resp.status_code >= 400:
            log.error("realtime session creation failed: %s", resp.text)
            raise HTTPException(resp.status_code, resp.text)

        from fastapi import Response
        return Response(content=resp.text, media_type="application/sdp")

    @router.post("/realtime/tool-call")
    async def realtime_tool_call(request: Request):
        """Called by the browser's data-channel handler when the model
        invokes a function. Routes through the exact same registry as
        the Claude path (skills.py) — one place that knows how to reach
        tany-bridge, not two."""
        body = await request.json()
        name = body.get("name")
        args = body.get("arguments") or {}
        user_id = body.get("user_id") or None

        skill = registry.get(name)
        if not skill:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            result = await skill.handler(args, user_id)
            return {"ok": True, "result": result}
        except Exception as e:
            log.exception("realtime tool call %s failed", name)
            return {"ok": False, "error": str(e)}

    app.include_router(router)
