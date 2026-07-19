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
VOICE_SVC_URL = os.environ.get("VOICE_SVC_URL", "http://voice-id-svc:8002")


# Client-side-only tool — dashboard.html intercepts this call itself
# (closes the WebRTC session after the farewell line finishes playing)
# instead of routing it to /realtime/tool-call like the real skills.
END_CONVERSATION_TOOL = {
    "type": "function",
    "name": "end_conversation",
    "description": "קורא לפונקציה הזו כשהמשתמש מסמן שהשיחה נגמרה (למשל 'תודה, סיימתי', 'זהו תודה', 'להתראות'). אמור קודם משפט סיום קצר וחם, ואז קרא לפונקציה.",
    "parameters": {"type": "object", "properties": {}},
}

# only added to the tool list when the face check at trigger time had a
# guess but wasn't confident enough to treat as certain — the model asks
# the guess as a yes/no question instead of either silently guessing
# (wrong name = awkward) or greeting generically (ignores a decent guess)
CONFIRM_IDENTITY_TOOL = {
    "type": "function",
    "name": "confirm_identity",
    "description": "קורא לפונקציה הזו מיד אחרי שהמשתמש ענה על שאלת הזיהוי הפותחת ('אני חושב שאתה X, נכון?'). confirmed=true אם אישר, false אם הכחיש או תיקן.",
    "parameters": {
        "type": "object",
        "properties": {"confirmed": {"type": "boolean"}},
        "required": ["confirmed"],
    },
}


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

    def _instructions_for(user_id: str) -> str:
        """Built once identity is certain (confirmed by voice or by the
        user answering the opening question) — the model's next
        session.update instructions, no greeting/confirm-identity
        wrapper needed since the conversation is already underway."""
        base_personality = load_base_personality(r)
        user_profile = load_user_profile(r, user_id)
        return build_system_prompt(base_personality, user_profile, user_id)

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
        tools.append(END_CONVERSATION_TOOL)

        # The greeting used to be a separate call through the old TTS
        # pipeline (a different voice/API from the Realtime session's own
        # voice) — folded it into this session's own first turn instead,
        # so there's one continuous voice from "שלום" onward. dashboard.html
        # triggers this opening turn as soon as the data channel is ready.
        #
        # Three cases: certain (face match was confident) -> greet by
        # name directly. Uncertain but there's a guess (best_guess from
        # vision-id-svc, just below the confidence floor) -> ask it as a
        # yes/no question instead of silently guessing wrong or ignoring
        # a decent guess entirely. No guess at all -> generic greeting,
        # same as before.
        if certain and user_id:
            greeting_instruction = (
                f'פתח את השיחה מיד באמירת "שלום {user_id}" ותו לא, ואז המתן שהמשתמש ידבר.'
            )
        elif user_id:
            greeting_instruction = (
                f'פתח את השיחה מיד בשאלה קצרה: "היי, אני חושב שאתה {user_id}, נכון?" ותו לא, '
                "ואז המתן לתשובה. ברגע שהמשתמש עונה (בין אם אישר, הכחיש, או תיקן את השם) — "
                "קרא מיד לפונקציה confirm_identity עם התוצאה, ורק אז המשך בשיחה."
            )
            tools.append(CONFIRM_IDENTITY_TOOL)
        else:
            greeting_instruction = (
                'פתח את השיחה מיד באמירת "שלום" ותו לא, ואז המתן שהמשתמש ידבר.'
            )
        end_instruction = (
            'כשהמשתמש אומר משהו שמסמן שהשיחה נגמרה (למשל "תודה, סיימתי", "זהו תודה", "להתראות") — '
            "אמור משפט סיום קצר וחם ואז קרא לפונקציה end_conversation."
        )
        system_prompt = f"{system_prompt}\n\n{greeting_instruction}\n{end_instruction}"

        session_config = {
            "type": "realtime",
            "model": REALTIME_MODEL,
            "instructions": system_prompt,
            "audio": {
                # slightly less trigger-happy than the 0.5 default —
                # belt-and-suspenders against a laptop's mic picking up
                # its own speaker mid-reply and barging in on itself.
                # The real fix is client-side echoCancellation
                # (dashboard.html); this just adds margin on top of it.
                "input": {"turn_detection": {"type": "server_vad", "threshold": 0.6, "silence_duration_ms": 600}},
                "output": {"voice": REALTIME_VOICE},
            },
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

    @router.post("/realtime/identify-voice")
    async def identify_voice(request: Request):
        """The realtime path has no server-side leg to run the Claude
        path's mid-conversation voice confirmation on (audio never
        touches this backend), so dashboard.html grabs a short raw PCM
        sample of its own mic input and posts it here once, early in the
        call, if the face check at trigger time wasn't certain. On a hit,
        returns fresh instructions (built the same way as session
        creation) so the browser can push them into the live session via
        session.update instead of restarting the call."""
        body = await request.json()
        audio_b64 = body.get("audio_b64")
        sample_rate = body.get("sample_rate", 16000)
        if not audio_b64:
            return {"certain": False, "error": "no audio"}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{VOICE_SVC_URL}/identify",
                    json={"audio_b64": audio_b64, "sample_rate": sample_rate},
                )
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            log.exception("voice-id call failed")
            return {"certain": False, "error": str(e)}

        if result.get("certain"):
            user_id = result["best_guess"]
            result["user_id"] = user_id
            result["instructions"] = _instructions_for(user_id)
        return result

    @router.post("/realtime/confirm-identity")
    async def confirm_identity(request: Request):
        """dashboard.html calls this after the model calls the
        confirm_identity tool with confirmed=true — the user answered
        "yes" to the opening "I think you're X, right?" question. Same
        shape as identify-voice's certain branch: fresh instructions to
        push into the live session via session.update."""
        body = await request.json()
        user_id = body.get("user_id")
        if not user_id:
            raise HTTPException(400, "user_id required")
        return {"user_id": user_id, "instructions": _instructions_for(user_id)}

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
