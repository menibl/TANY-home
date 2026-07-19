"""
Single point of contact with TANY. Nothing else in HomeBot is allowed to
know TANY's API shape — that isolation is what lets TANY's own API evolve
without touching the orchestrator or skill registry.

TANY exposes one MCP tool (tany_command) that takes free-form text and
routes it internally — auth is a per-user Bearer token (each person gets
their own from TANY), passed in per request by the caller (orchestrator's
skills.py, which looks it up from that user's stored profile). This
service holds no credentials of its own.
"""
import logging
import os

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [tany-bridge] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="tany-bridge")

TANY_MCP_URL = os.environ.get("TANY_MCP_URL", "https://tany-helper.com/mcp/home")


class CommandRequest(BaseModel):
    text: str
    tany_token: str


async def _call_tany(text: str, token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TANY_MCP_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "method": "tools/call",
                "params": {"name": "tany_command", "arguments": {"text": text}},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["result"]["content"][0]["text"]
        return {"ok": True, "result": content}


@app.post("/command")
async def command(req: CommandRequest):
    try:
        return await _call_tany(req.text, req.tany_token)
    except httpx.HTTPStatusError as e:
        log.error("TANY call failed: %s %s", e.response.status_code, e.response.text)
        return {"ok": False, "error": f"TANY error {e.response.status_code}"}
    except Exception as e:
        log.exception("TANY call failed")
        return {"ok": False, "error": str(e)}
