# HomeBot

עוזר קולי ביתי: מחיאת כף כפולה -> זיהוי אדם מתמונה -> אישוש/זיהוי לפי קול
-> שיחה חיה -> ביצוע פקודות דרך TANY. פרויקט עצמאי, לא תלוי ב-MAI FOCUS.

## שלבים (מצב נוכחי)

| שלב | שירות | סטטוס |
|---|---|---|
| 1. מחיאת כף כפולה + snapshot | `capture-svc` | פונקציונלי — clap detector + RTSP grab מוכנים |
| 2. זיהוי אדם מתמונה | `vision-id-svc` | face embedding עובד; soft-biometrics (גוף/שיער) = TODO |
| 3. זיהוי לפי קול | `voice-id-svc` | פונקציונלי (Resemblyzer) |
| 4. שיחה חיה | `orchestrator` | STT (faster-whisper) + Claude+tools עובדים; TTS = placeholder, צריך Piper/ElevenLabs |
| 5. חיבור ל-TANY | `tany-bridge` | שלד מוכן, `_call_tany()` צריך להתחבר ל-endpoints האמיתיים |

## הרצה ראשונית (הכל על מחשב אחד)

```bash
cp .env.example .env   # למלא RTSP_URL + ANTHROPIC_API_KEY לפחות
docker compose up --build
```

## להעביר שירות למחשב/Raspberry Pi אחר

1. להריץ את אותו Dockerfile על המכונה החדשה, לחשוף את הפורט שלו.
2. לעדכן את משתנה ה-URL המתאים אצל מי שקורא לו (למשל אם `vision-id-svc`
   עובר למכונה `192.168.1.50`, מעדכנים ב-`capture-svc`:
   `VISION_SVC_URL=http://192.168.1.50:8001`).
3. שום שינוי קוד לא נדרש — כל השירותים מדברים רק דרך HTTP/WebSocket
   (ראה `shared/API_CONTRACTS.md`).

`capture-svc` צריך תמיד להישאר על המכונה שמחוברת פיזית למיקרופון/רמקול/מצלמה.
כל השאר ניתן להעברה חופשית.

## מה עוד חסר לפני production

- TTS אמיתי ב-`orchestrator/speech_io.py::synthesize` (Piper מומלץ להתחלה — חינמי ולוקאלי)
- soft-biometrics אמיתי ב-`vision-id-svc/main.py::_soft_biometrics` (MediaPipe Pose + hair classifier)
- endpoint-ים אמיתיים ב-`tany-bridge/main.py::_call_tany`
- תהליך enrollment (הקלטת/צילום כל בן בית בפעם הראשונה) — כרגע יש רק `/enroll` API גולמי, אין CLI/UI נוח
- הגדרת `bot:base_personality` ו-`user:<id>:personality` הראשוניים ב-Redis
