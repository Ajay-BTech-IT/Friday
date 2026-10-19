# Personal Agent (PC build)

Autonomous backend for the FRIDAY personal agent: persona chat, shared memory, tasks/reminders/routines,
calendar, meals, briefings, Gmail digest and draft approval. Follows `../Architecture_Plan.txt` and
`../Tables.txt`. This build is **PC only**: no FCM, no multi-device, no phone features, API token optional.

## Run it

1. Postgres with pgvector (Supabase / Neon / local). The DB user must be allowed `CREATE EXTENSION`.
2. Edit `.env` (already copied from `.env.example`). **Required:** `DATABASE_URL`, `GOOGLE_API_KEY`
   (plus `OWNER_EMAIL`, `OWNER_NAME`, `OWNER_TIMEZONE`).
3. Start (creates every table, seeds the owner, a neutral default persona and 27 Indian foods by itself):
   ```
   .venv\Scripts\python run.py
   ```
   Always use `run.py` on Windows: uvicorn's default loop breaks psycopg async.
4. Talk to it in a second terminal: `.venv\Scripts\python cli.py`
   (reminders and briefings pop up there and as Windows toasts). API docs: http://127.0.0.1:8000/docs

## What you fill in `.env`

| Variable | Needed for |
|---|---|
| `DATABASE_URL` | everything |
| `GOOGLE_API_KEY`, `GEMINI_MODEL`, `EMBEDDING_MODEL` | chat, memory, extraction |
| `OWNER_*` | the single user |
| `GOOGLE_CLIENT_ID/SECRET`, `GOOGLE_REDIRECT_URI`, `TOKEN_ENCRYPTION_KEY` | Gmail + Calendar (optional). Then open `/auth/google/start` once. |
| `DAILY_NOTIFY_CAP`, `OVERNIGHT_JOB_TIME`, `EVENING_BRIEFING_TIME`, poll seconds | proactivity tuning |
| `API_AUTH_TOKEN` | leave empty on the PC; set when exposing it (then every endpoint needs `Authorization: Bearer ...`) |
| `MURF_*`, FCM | held for the voice / mobile phase |

## How the autonomy works

- **One agent, 37 tools** (`tools/__init__.py`): profile, tasks, routines, calendar, meals, memory, Gmail
  (read + draft), briefings, and generic database access.
- **Say it casually, it acts.** "need to buy eggs" becomes a task plus reminder (`due_hint` is turned into a
  real time from your office hours). A background extractor also catches commitments/facts the chat missed.
- **Ask, then apply.** "update my office timing" with no values: it asks one question and changes nothing.
  Your answer ("10 to 7") is applied through `update_profile`, and it replies old -> new. Every autonomous write
  is stored in `audit_log` (`GET /audit`).
- **Database access = select, insert, update. Never delete.** `database/generic.py` gives the agent
  `db_describe / db_select / db_insert / db_update` on a whitelist of tables with per-table writable columns.
  `oauth_credentials`, `users` and migrations are unreachable; `user_id` is injected; updates are by id only.
  `database/db.py` also rejects any DELETE/DROP/TRUNCATE/ALTER statement at runtime, so even a bug cannot
  remove data. Removal = status change (`done`, `cancelled`, `ignored`, `active=false`, memory `valid_to`).
- **Email is two-step.** The agent has no send tool. It creates a draft (`pending_approval`); you
  `POST /drafts/{id}/approve` (or `/approve <id>` in the CLI) and only then does the API send it.
  Approval fields are not writable by the agent.

## Flows implemented

Morning briefing (precomputed at 03:30, delivered at your wake time) Â· casual mention -> reminder Â·
schedule + remind (calendar event + morning-of + 30-min-before reminders + linked task, synced to Google if
connected) Â· Gmail overnight digest, importance 1-5, action items to tasks, draft/approve/send Â· meals
(catalog first, Gemini estimate otherwise, corrections become preferences) Â· persona switch without an LLM
call Â· evening wrap-up Â· quiet hours (sleep -> wake), daily cap, grouped reminders.

## Local embeddings (memory search)
Memory vectors come from a local model, `nomic-embed-text` (768 dimensions) running in Ollama, so nothing is sent to
Google for embeddings. Install once: Ollama from https://ollama.com/download, then `ollama pull nomic-embed-text`.
`.env`: `EMBEDDING_PROVIDER=ollama`, `EMBEDDING_MODEL=nomic-embed-text`, `OLLAMA_URL=http://localhost:11434`.
Every memory records which model made its vector; on start the server re-embeds any memory from a different model
(for example the old Gemini ones). To go back set `EMBEDDING_PROVIDER=gemini` and `EMBEDDING_MODEL=gemini-embedding-001`.
`/health` shows the active embedder. While Ollama is unreachable, chat still works but memory search/save pauses.

## Voice

Every reply streams as text AND audio (Murf Falcon, `POST /chat/stream`; `TTS_MODE=full` sends the whole reply to Murf in one request, `sentence` speaks sentence by sentence: events `start`, `token`, `audio` (base64 MP3 per sentence, in order), `done`). Text appears immediately; the first sentence is spoken about a second later while the rest is still being written. Reminders and briefings can be voiced with `POST /chat/tts`. Any client (PC CLI now, Flutter later) just plays the `audio` events in `seq` order. If Murf fails the reply continues as text. In the CLI: `/voice off` mutes. Each persona stores its own voice in the `personas` table; tell the agent "switch to FRIDAY", "your name is PRIME" or "use a deeper voice" and it creates/renames/retunes the persona itself (tools: switch_persona, create_persona, update_persona, list_personas, list_voices). `MURF_VOICE_MALE` / `MURF_VOICE_FEMALE` are only fallbacks for personas without a voice.

## Held for later (as requested)

FCM and multi-device, API token generation, Flutter app, call-by-name, missed calls/messages (`device_events`,
`contacts` rows exist but no ingestion), microphone/speech-to-text, photo meal logging.

## Deviations from `Tables.txt`

`schema.sql` is generated from it (made idempotent) and adds: `personas` + `user_profile.active_persona_id`
(the architecture needs them), `notifications` (in-app inbox replacing push), `audit_log`, and two extra indexes.
Drop numbered `.sql` files into `database/migrations/` for later changes.

## Known limits

- Reminders only fire while `run.py` is running; on restart it catches up (stale routine reminders are dropped).
- Windows toasts need `winotify` (installed); set `DESKTOP_NOTIFICATIONS=false` to disable.
- Not yet run against live Gemini/pgvector/Google: see the test notes in the hand-off message.

## Face + conversation page

Open **http://127.0.0.1:8000/ui/** while the server runs. A glowing face on the left, the chat on the right (type, or use the mic;
replies are also spoken and the mouth follows the voice; YouTube requests play inside the page).
The face is not scripted: after each reply the active persona is asked (`POST /chat/expression`, see `orchestrator/expression.py`)
how its face looks while saying it, and it returns numbers (eyes, brows, mouth, head tilt, gaze, glow colour) plus an emoji and a
mood word that the page simply draws. Switch persona and the face changes with the character. The first time, open the gear icon
and paste `API_AUTH_TOKEN` if your `.env` has one. "Expression strength" in settings only scales how far the face moves.

The page's lip sync (`web/lipsync.js`) analyses the voice while it plays: loudness and the low formant band open the jaw, the
vowel's frequency balance spreads or rounds the lips, hissing sounds show teeth, and pauses close the mouth. Controls under the face:
Voice on/off, Stop (or Esc), Speak (click to talk), Hands-free (experimental: listens and answers when you pause), volume,
Focus (face + subtitles only), Fullscreen, Settings. With voice off the server is not asked for audio at all.

### The 3D head
`web/avatar3d.js` draws a 3D head with three.js (files in `web/vendor/`, no CDN needed): a scanned human head with the 52 standard facial
shape keys, a skin tone you choose (Settings: 8 swatches, hair and eye colour) with procedural pores and warm shading, strand-rendered
hair and eyebrows, and glossy eyes. The same expression numbers and lip-sync values drive the shape keys. If WebGL or the model is not
available the page falls back to the drawn face automatically. **The 3D head is opt-in: open `/ui/?3d=1`.** By default the page uses the lighter drawn face (nothing from `web/vendor/` is loaded). The head model is "Face Cap" from the
three.js examples; check its terms before redistributing the project (see `web/vendor/README.txt`).


### Voice latency (`TTS_MODE`)
`sentence` (default): each sentence is spoken as soon as it is written, so the voice starts about half a second after the reply
starts. `full`: the whole reply is sent to Murf at once; it sounds slightly smoother but starts only after the text is finished
(measured 1.6 to 4.8 seconds later). Set `TTS_MODE=full` in `.env` to go back. The page plays the chunks back to back without gaps.
