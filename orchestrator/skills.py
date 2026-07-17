"""
Skill registry. Each skill is just a name + which tool-schema to expose to
the LLM + where to route the call when the LLM invokes it. Adding a new
skill (smart-home, bank, email) later means adding one entry here and one
bridge service — the orchestrator's core loop never changes.
"""
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import httpx


@dataclass
class Skill:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, str | None], Awaitable[dict]]


async def _tany_command(tany_bridge_url: str, user_id: str | None, intent: str, args: dict, raw_text: str = "") -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{tany_bridge_url}/command",
            json={"user_id": user_id, "intent": intent, "args": args, "raw_text": raw_text},
        )
        resp.raise_for_status()
        return resp.json()


def build_registry(tany_bridge_url: str) -> dict[str, Skill]:
    async def add_to_shopping_list(args: dict, user_id: str | None) -> dict:
        return await _tany_command(tany_bridge_url, user_id, "shopping_list.add", args)

    async def create_reminder(args: dict, user_id: str | None) -> dict:
        return await _tany_command(tany_bridge_url, user_id, "reminders.create", args)

    async def dictate_note(args: dict, user_id: str | None) -> dict:
        return await _tany_command(tany_bridge_url, user_id, "notes.create", args)

    registry = {
        "add_to_shopping_list": Skill(
            name="add_to_shopping_list",
            description="מוסיף פריט לרשימת הקניות ב-TANY",
            input_schema={
                "type": "object",
                "properties": {"item": {"type": "string"}},
                "required": ["item"],
            },
            handler=add_to_shopping_list,
        ),
        "create_reminder": Skill(
            name="create_reminder",
            description="יוצר תזכורת ב-TANY",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "when": {"type": "string", "description": "e.g. 'מחר בבוקר', ISO date, etc."},
                },
                "required": ["text"],
            },
            handler=create_reminder,
        ),
        "dictate_note": Skill(
            name="dictate_note",
            description="מקליט ושומר הערה חופשית ב-TANY, בלי לפרש אותה",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=dictate_note,
        ),
        # TODO next skills to add here, each with its own bridge service:
        #   smart_home.set_ac / smart_home.set_boiler / smart_home.play_music
        #   bank.get_balance (requires elevated auth scope + re-confirmation)
        #   email.read_unread / email.send
    }
    return registry


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
