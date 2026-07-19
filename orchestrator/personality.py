"""
Two layers, merged into one system prompt per session:
  1. bot base personality  - shared tone/character across all users
  2. per-user profile      - how THIS bot should behave for THIS person
     (formality, language, nicknames, which skills they're allowed to use)

Mirrors how per-customer settings already work in TANY, so the same
mental model carries over.
"""
import json
import redis

DEFAULT_BASE_PERSONALITY = {
    "name": "הבית",
    "tone": "חם, ישיר, לא מציף במילים מיותרות",
    "language": "he",
}

DEFAULT_USER_PROFILE = {
    "formality": "casual",
    "nickname": None,
    "skills_enabled": ["tany_command"],
    # per-user token from TANY (Bearer, tanyhome_...) — TANY routes
    # everything internally through one MCP tool, so there's nothing to
    # scope per-skill here, just per-person auth
    "tany_token": None,
}


def _r(profile_store_url: str) -> redis.Redis:
    return redis.from_url(profile_store_url)


def load_base_personality(r: redis.Redis) -> dict:
    raw = r.get("bot:base_personality")
    return json.loads(raw) if raw else DEFAULT_BASE_PERSONALITY


def load_user_profile(r: redis.Redis, user_id: str | None) -> dict:
    if user_id is None:
        return DEFAULT_USER_PROFILE
    raw = r.get(f"user:{user_id}:personality")
    if not raw:
        return DEFAULT_USER_PROFILE
    # merge over the default so older stored profiles (saved before a
    # field like tany_token existed) still come back with every key
    return {**DEFAULT_USER_PROFILE, **json.loads(raw)}


def save_user_profile_fields(r: redis.Redis, user_id: str, **fields) -> dict:
    """Merge-updates the stored profile with the given fields, without
    clobbering whatever else is already saved for this user."""
    profile = load_user_profile(r, user_id)
    profile.update(fields)
    r.set(f"user:{user_id}:personality", json.dumps(profile, ensure_ascii=False))
    return profile


def build_system_prompt(base: dict, user: dict, user_id: str | None) -> str:
    who = user_id or "אורח לא מזוהה"
    nickname = user.get("nickname") or who
    return (
        f"את/ה {base['name']}, עוזר בית קולי. סגנון הדיבור שלך: {base['tone']}.\n"
        f"אתה מדבר כרגע עם {nickname}. רמת פורמליות מועדפת: {user['formality']}.\n"
        f"סקילים זמינים למשתמש הזה: {', '.join(user['skills_enabled'])}.\n"
        f"ענה תמיד בעברית, במשפטים קצרים וטבעיים כמו בשיחה אמיתית, לא כמו מסמך."
    )
