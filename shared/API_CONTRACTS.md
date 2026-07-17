# HomeBot — Internal API Contracts

All calls are plain HTTP + JSON. Every service is stateless except
`profile-store` (Redis) which holds embeddings and per-user settings.
Keeping the contracts here (not buried in code) means any service can be
rewritten in a different language later without breaking anyone else.

---

## capture-svc -> vision-id-svc
`POST /identify` on vision-id-svc, called right after a double-clap + RTSP
snapshot.

Request:
```json
{
  "image_b64": "<jpeg base64>",
  "captured_at": "2026-07-17T18:04:00Z"
}
```

Response:
```json
{
  "candidates": [
    {"user_id": "meni", "confidence": 0.81, "signals": {"face": 0.9, "body": 0.6, "hair": 0.7}},
    {"user_id": "yonatan", "confidence": 0.22, "signals": {"face": 0.0, "body": 0.3, "hair": 0.1}}
  ],
  "best_guess": "meni",
  "confidence": 0.81,
  "certain": true
}
```
`certain: false` when `confidence < MATCH_THRESHOLD` -> capture-svc should
greet with a plain "שלום" and let voice-id confirm during the conversation.

---

## capture-svc / orchestrator -> voice-id-svc
`POST /identify` — same shape response as vision-id-svc, but input is a
short audio clip instead of an image:
```json
{ "audio_b64": "<wav/pcm16 base64>", "sample_rate": 16000 }
```

`POST /enroll` — adds a new reference sample for a user:
```json
{ "user_id": "meni", "audio_b64": "<wav>", "sample_rate": 16000 }
```

---

## capture-svc <-> orchestrator (the live conversation)
This is a WebSocket, not plain REST, because audio needs to stream both
ways continuously once the session opens.

`WS /session?user_id=meni&certain=true`

Frames sent capture-svc -> orchestrator: raw PCM16 audio chunks (~100ms each).
Frames sent orchestrator -> capture-svc:
```json
{"type": "partial_transcript", "text": "..."}
{"type": "identity_update", "user_id": "meni", "confidence": 0.88}
{"type": "audio_chunk", "audio_b64": "..."}
{"type": "tool_call", "name": "add_to_shopping_list", "status": "running"}
{"type": "end_of_turn"}
```

`identity_update` is how voice-id (stage 3) can silently correct or confirm
the person mid-conversation without capture-svc needing to know how that
happened.

---

## orchestrator -> tany-bridge
`POST /command`
```json
{
  "user_id": "meni",
  "intent": "shopping_list.add",
  "args": {"item": "חלב"},
  "raw_text": "תגיד לטאני להכניס חלב לרשימת קניות"
}
```
Response:
```json
{"ok": true, "result": "נוסף חלב לרשימת הקניות"}
```

`tany-bridge` is the ONLY service allowed to hold TANY credentials/MCP
session. Nothing else talks to TANY directly.

---

## profile-store (Redis) key layout
```
user:<user_id>:face_embeddings      -> list of vectors (JSON)
user:<user_id>:voice_embeddings     -> list of vectors (JSON)
user:<user_id>:personality          -> JSON {tone, formality, language, ...}
user:<user_id>:skills_enabled       -> JSON list ["shopping_list", "reminders", ...]
bot:base_personality                -> JSON (shared across all users)
```
