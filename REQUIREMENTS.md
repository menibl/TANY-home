# HomeBot — דרישות מלאות (למסירה ל-Claude Code)

Repo: `git@github.com:menibl/TANY-home.git`
פרויקט **עצמאי**, ללא תלות בשירותים/תשתית של MAI FOCUS.

## הרעיון הכללי
מערכת שיושבת על מחשב בבית, מאזינה ברקע, ובזיהוי מחיאת כף כפולה פותחת
זיהוי ויזואלי של בן הבית מול מצלמת RTSP, מברכת אותו בשמו, ואז עוברת
לשיחה קולית רציפה (כמו live mode של ChatGPT/Claude/Gemini) שמחוברת
לפקודות בפועל (TANY, ובעתיד גם בית חכם).

## שלבי המימוש (בסדר הזה)

1. **זיהוי מחיאת כף כפולה + snapshot**
   - מאזין תמידי למיקרופון, מזהה שתי מחיאות כף קרובות בזמן (חלון מוגדר, ~600ms)
   - זיהוי מבוסס אנרגיית אודיו — **בלי מודל ML**, כדי שיהיה קל למחשב הישן
   - בזיהוי הטריגר: לוקח 1-2 snapshots ממצלמת RTSP ביתית

2. **זיהוי אדם מהתמונה**
   - לא חייב זיהוי פנים בלבד — **שילוב פרמטרים**: פנים (אם יש), מבנה גוף,
     מגדר משוער, צבע/אורך שיער
   - כל פרמטר מקבל ציון confidence, ומתמזג ל-fusion score משוקלל
   - אם confidence מעל סף — מברך בשם ("שלום מני")
   - אם מתחת לסף — מברך "שלום" סתמי, וממשיך לזיהוי בשלב 3

3. **זיהוי/אישוש לפי קול**
   - fallback ומאשש: כשלא זוהה בביטחון מהתמונה, ברגע שהאדם מתחיל לדבר
     המערכת בודקת את טביעת הקול מול פרופילים שמורים
   - ברגע שעובר סף ביטחון — "נועלת" זהות גם באמצע השיחה (retroactively)

4. **שיחה חיה (live mode)**
   - שיחה קולית רציפה, לא turn-by-turn — סטרימינג דו-כיווני
   - כוללת אישיות לבוט: שכבת "אישיות בסיס" (טון כללי) + שכבת "פרופיל
     משתמש" (פורמליות, כינוי, אילו סקילים מותרים) — **בדומה למנגנון
     ההגדרות הקיים ב-TANY לכל לקוח**
   - מותאמת לזהות שזוהתה בשלבים 2-3 (קונטקסט אישי לכל בן בית)

5. **חיבור ל-TANY (מנוע הפעולות)**
   - שימוש במנוע TANY הקיים (יש לו כבר MCP server + OpenAPI spec) לביצוע
     פקודות בפועל
   - דוגמאות פקודה: "תגיד לטאני להכניס חלב לרשימת קניות",
     "תקליט עכשיו מה שאני אומר ותשלח לטאני" (הקלטת הערה חופשית)
   - עוד דוגמאות פקודה יתווספו בהמשך

## יעד עתידי (לא בשלב הנוכחי, אבל הארכיטקטורה חייבת לתמוך בזה)
- שילוב עם אלמנטים של בית חכם: דוד שמש, מזגן, מוזיקה
- סקילים לגישה למידע אישי: התחברות לבנק, מייל, ועוד
- שליטה מלאה בבית דרך העוזר הקולי הזה
- **המשמעות האדריכלית**: המערכת חייבת לתמוך בהוספת "שכבות skill" חדשות
  בלי לשנות את הליבה — כל skill חדש = handler + לפעמים bridge service
  נפרד, לא refactor

## מגבלות סביבת ההרצה
- **מחשב ישן/חלש** — קריטי שלא יקרוס. הפתרון שנבחר:
  - כל שירות רץ כ-**Docker container נפרד**
  - כרגע כולם רצים על אותו מחשב ישן, אבל **כל container ניתן להעברה**
    בעתיד ל-Raspberry Pi / מחשב אחר / שרת חיצוני **על אותה רשת מקומית**
  - שירותים מדברים אך ורק דרך HTTP/WebSocket ברשת הפנימית (docker
    network), **אף פעם לא דרך shared filesystem/memory** — כדי שהעברת
    שירות למכונה אחרת תהיה רק שינוי URL ב-env var, בלי לגעת בקוד
  - `capture-svc` (מיקרופון/רמקול/snapshot) הוא היחיד שחייב להישאר
    פיזית על המחשב המחובר לחומרה — כל השאר רלוקטבילי

## העדפת LLM (בסדר עדיפות)
1. **Claude** — עדיפות ראשונה
2. ChatGPT
3. Gemini

**הערה טכנית חשובה:** ל-Claude (Anthropic API) אין כרגע API של
speech-to-speech native כמו OpenAI Realtime או Gemini Live. המשמעות:
מול Claude השיחה חייבת להיות **pipeline מדורג** — STT חיצוני (למשל
faster-whisper) → Claude Messages API עם tool-calling (טקסט) → TTS
חיצוני (למשל Piper). latency מעט גבוה יותר מ-speech-to-speech ישיר,
אבל מאפשר להשתמש ב-Claude. האדפטר ל-LLM חייב להיות **פלאגבילי** —
מתג `LLM_PROVIDER=claude|openai|gemini` בלי לשנות את שאר הקוד, כדי
שאם בעתיד ירצה לעבור ל-OpenAI Realtime/Gemini Live (speech-to-speech
אמיתי) זה יהיה מתג ולא refactor.

## תהליך העבודה (git-based)
- כל הקוד חי ב-`git@github.com:menibl/TANY-home.git`
- זרימה: כתיבה/עדכון קוד → commit → push → **על המחשב הישן: `git pull`
  + `docker compose up -d --build`**
- לא לעבוד עם zip-ים ידניים כזרימת עבודה קבועה — Claude Code אמור
  לעבוד ישירות מול ה-repo המקומי עם git אמיתי

## מה כבר בנוי (בשלד הראשוני שהועלה)
- `capture-svc` — clap detector עובד (energy-based) + RTSP snapshot
  grabber + client שמדבר עם vision-id ואז פותח WebSocket session מול
  ה-orchestrator
- `vision-id-svc` — face embedding עובד (face_recognition) + fusion
  scoring; **soft-biometrics (גוף/שיער) עדיין TODO/stub**
- `voice-id-svc` — Resemblyzer, enroll+identify מול Redis, עובד
- `orchestrator` — STT (faster-whisper) + Claude עם tool-calling +
  שכבת אישיות (base + per-user) + מנגנון אישוש קול תוך-כדי-שיחה
  (`identity_update`); **TTS הוא placeholder (שקט) — צריך Piper/ElevenLabs**
- `tany-bridge` — שירות בידוד יחיד שמחזיק קרדנצ'לס של TANY, שלד מוכן
  ל-3 אינטנטים (shopping_list.add, reminders.create, notes.create),
  **אבל `_call_tany()` עדיין stub — לא מחובר לאנדפוינטים האמיתיים של TANY**
- `profile-store` — Redis, סכמת מפתחות מוגדרת ב-`shared/API_CONTRACTS.md`
- כל חוזי ה-API בין השירותים מתועדים במלואם ב-`shared/API_CONTRACTS.md`

## מה חסר / המשימות הבאות
1. TTS אמיתי ב-`orchestrator/speech_io.py::synthesize`
2. soft-biometrics אמיתי ב-`vision-id-svc/main.py::_soft_biometrics`
   (MediaPipe Pose למבנה גוף + classifier לצבע/אורך שיער)
3. חיבור אמיתי ל-TANY ב-`tany-bridge/main.py::_call_tany` (יש כבר
   MCP server + OpenAPI spec ב-TANY עצמו — צריך רק לחווט)
4. תהליך enrollment נוח (הקלטת קול + צילום ראשוני לכל בן בית) — כרגע
   יש רק endpoint גולמי `/enroll`, אין CLI/UI
5. אתחול נתוני ברירת מחדל ב-Redis: `bot:base_personality` ופרופיל
   ראשוני לכל בן בית (`user:<id>:personality`)
6. `docker-compose.remote.yml` לדוגמה — הדגמה בפועל של העברת שירות
   אחד למכונה אחרת ברשת (טרם נבנה)
7. פקודות TANY נוספות מעבר לשלוש הראשונות (יתווספו לפי דוגמאות מ-Meni)
