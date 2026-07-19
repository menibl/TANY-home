"""
Skill registry. Each skill is just a name + which tool-schema to expose to
the LLM + where to route the call when the LLM invokes it.

TANY exposes exactly one MCP tool (tany_command) that takes free-form text
and routes it internally (shopping list, reminders, calendar, whatever TANY
already knows how to do) — so there's a single generic skill here rather
than one entry per TANY capability. Auth is per-person: each user has their
own TANY token (obtained from TANY, entered once via /enroll), stored in
their profile in profile-store.
"""
from dataclasses import dataclass
from typing import Callable, Awaitable
import httpx
import redis

from personality import load_user_profile


@dataclass
class Skill:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, str | None], Awaitable[dict]]


def build_registry(tany_bridge_url: str, r: redis.Redis) -> dict[str, Skill]:
    async def tany_command(args: dict, user_id: str | None) -> dict:
        if not user_id:
            return {"ok": False, "error": "לא מזוהה — צריך לדעת מי מדבר כדי להשתמש ב-TANY"}
        profile = load_user_profile(r, user_id)
        token = profile.get("tany_token")
        if not token:
            return {"ok": False, "error": f"אין עדיין טוקן TANY שמור עבור {user_id} — צריך להזין אותו במסך ההרשמה"}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{tany_bridge_url}/command",
                json={"text": args.get("text", ""), "tany_token": token},
            )
            resp.raise_for_status()
            return resp.json()

    return {
        "tany_command": Skill(
            name="tany_command",
            description=(
                "שולח כל בקשה או פקודת ניהול-בית (רשימת קניות, תזכורות, יומן, ועוד) "
                "ל-TANY, שכבר יודע לטפל בזה. שלח את הבקשה כפי שהמשתמש ניסח אותה, בעברית."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "הבקשה בשפה חופשית, כפי שהמשתמש אמר אותה"}
                },
                "required": ["text"],
            },
            handler=tany_command,
        ),
        # TODO next skills to add here, each with its own bridge service
        # (only if they don't already go through TANY's tany_command):
        #   smart_home.set_ac / smart_home.set_boiler / smart_home.play_music
        #   bank.get_balance (requires elevated auth scope + re-confirmation)
    }


def enabled_tools_for(registry: dict[str, Skill], skills_enabled: list[str]) -> list[dict]:
    """Only expose to the LLM the tools this specific user is allowed to use."""
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "input_schema": skill.input_schema,
        }
        for name, skill in registry.items()
        if name in skills_enabled
    ]
