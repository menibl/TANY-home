"""
Single point of contact with TANY. Nothing else in HomeBot is allowed to
hold TANY credentials or know its API shape — that isolation is what lets
TANY's own API evolve without touching the orchestrator or skill registry.

Fill in _call_tany() per intent once you have TANY's actual OpenAPI/MCP
endpoints in front of you — the routing/validation shell here won't need
to change.
"""
import os

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="tany-bridge")

TANY_MCP_URL = os.environ.get("TANY_MCP_URL")
TANY_API_KEY = os.environ.get("TANY_API_KEY")


class CommandRequest(BaseModel):
    user_id: str | None
    intent: str
    args: dict
    raw_text: str = ""


INTENT_HANDLERS = {
    "shopping_list.add",
    "reminders.create",
    "notes.create",
}


async def _call_tany(intent: str, user_id: str | None, args: dict) -> dict:
    # TODO: replace with the real TANY MCP tool call / REST endpoint per intent.
    # Example shape once wired up:
    #
    # async with httpx.AsyncClient(timeout=10) as client:
    #     resp = await client.post(
    #         f"{TANY_MCP_URL}/tools/{intent}",
    #         headers={"Authorization": f"Bearer {TANY_API_KEY}"},
    #         json={"user_id": user_id, **args},
    #     )
    #     resp.raise_for_status()
    #     return resp.json()
    return {"ok": True, "result": f"[stub] would call TANY intent={intent} args={args} for user={user_id}"}


@app.post("/command")
async def command(req: CommandRequest):
    if req.intent not in INTENT_HANDLERS:
        return {"ok": False, "error": f"unknown intent: {req.intent}"}
    return await _call_tany(req.intent, req.user_id, req.args)
