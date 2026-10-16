-- =====================================================================
-- Personal Agent: Postgres + pgvector data model
-- GENERATED from FRIDAY/Tables.txt (made idempotent) + local additions at the bottom.
-- Conventions: UUID PKs, timestamptz in UTC, user_id on every table
-- (single user today, multi-user / RLS ready), soft-delete via status.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;

-- ---------------------------------------------------------------------
-- 1. IDENTITY, PROFILE, DEVICES
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         citext UNIQUE NOT NULL,
    display_name  text NOT NULL,
    timezone      text NOT NULL DEFAULT 'Asia/Kolkata',
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Core profile: small, structured, ALWAYS injected into the prompt.
CREATE TABLE IF NOT EXISTS user_profile (
    user_id         uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    wake_time       time,
    sleep_time      time,
    office_start    time,
    office_end      time,
    work_days       smallint[] DEFAULT '{1,2,3,4,5}',   -- ISO: 1=Mon..7=Sun
    commute_minutes int,
    persona         text,            -- tone/personality for the companion
    extra           jsonb NOT NULL DEFAULT '{}',
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS devices (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform    text NOT NULL CHECK (platform IN ('android','ios','web','desktop')),
    name        text,
    fcm_token   text,
    last_seen   timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Google OAuth tokens (encrypt at the app layer before storing)
CREATE TABLE IF NOT EXISTS oauth_credentials (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider           text NOT NULL DEFAULT 'google',
    scopes             text[] NOT NULL,
    refresh_token_enc  bytea NOT NULL,
    access_token_enc   bytea,
    expires_at         timestamptz,
    gmail_history_id   text,         -- incremental Gmail sync cursor
    calendar_sync_token text,        -- incremental Calendar sync cursor
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider)
);

-- People the agent can act on ("call Mom"). Phone numbers resolve on device.
CREATE TABLE IF NOT EXISTS contacts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          text NOT NULL,
    aliases       text[] NOT NULL DEFAULT '{}',   -- 'mom','amma'
    relationship  text,
    phone         text,
    email         citext,
    notes         text
);
CREATE INDEX IF NOT EXISTS contacts_alias_idx ON contacts USING gin (aliases);

-- ---------------------------------------------------------------------
-- 2. CONVERSATIONS (short-term memory + audit)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id   uuid REFERENCES devices(id),
    started_at  timestamptz NOT NULL DEFAULT now(),
    ended_at    timestamptz,
    summary     text                      -- rolling summary for context window
);

CREATE TABLE IF NOT EXISTS messages (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role             text NOT NULL CHECK (role IN ('user','assistant','tool','system')),
    content          text,
    tool_calls       jsonb,                -- function-call requests / results
    channel          text DEFAULT 'text' CHECK (channel IN ('text','voice')),
    extracted        boolean NOT NULL DEFAULT false,  -- memory extraction done?
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conv_idx ON messages (conversation_id, created_at);
CREATE INDEX IF NOT EXISTS messages_unextracted_idx ON messages (created_at) WHERE NOT extracted;

-- ---------------------------------------------------------------------
-- 3. LONG-TERM SEMANTIC MEMORY (RAG)
-- ---------------------------------------------------------------------
-- Embedding dim 768 (Gemini embedding with output dimensionality 768).
-- If you change the embedding model/dim, add a new column + reindex.
CREATE TABLE IF NOT EXISTS memories (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind               text NOT NULL CHECK (kind IN
                         ('fact','preference','episode','commitment','summary')),
    content            text NOT NULL,
    embedding          vector(768) NOT NULL,
    importance         real NOT NULL DEFAULT 0.5,     -- 0..1, used in re-ranking
    source_message_id  uuid REFERENCES messages(id) ON DELETE SET NULL,
    valid_from         timestamptz NOT NULL DEFAULT now(),
    valid_to           timestamptz,                   -- NULL = still true
    superseded_by      uuid REFERENCES memories(id),  -- fact updates keep history
    access_count       int NOT NULL DEFAULT 0,
    last_accessed_at   timestamptz,
    metadata           jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memories_embedding_idx ON memories
    USING hnsw (embedding vector_cosine_ops) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS memories_user_kind_idx ON memories (user_id, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS memories_fts_idx ON memories USING gin (to_tsvector('english', content));

-- ---------------------------------------------------------------------
-- 4. TASKS, REMINDERS, ROUTINES, CALENDAR
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_events (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    google_event_id  text,
    calendar_id      text DEFAULT 'primary',
    title            text NOT NULL,
    description      text,
    start_at         timestamptz NOT NULL,
    end_at           timestamptz,
    all_day          boolean NOT NULL DEFAULT false,
    rrule            text,
    etag             text,
    status           text NOT NULL DEFAULT 'confirmed'
                       CHECK (status IN ('confirmed','tentative','cancelled')),
    created_by       text NOT NULL DEFAULT 'agent' CHECK (created_by IN ('agent','user','sync')),
    UNIQUE (user_id, google_event_id)
);
CREATE INDEX IF NOT EXISTS calendar_events_time_idx ON calendar_events (user_id, start_at);

CREATE TABLE IF NOT EXISTS tasks (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title              text NOT NULL,
    notes              text,
    status             text NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','done','snoozed','cancelled')),
    priority           smallint NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 4),
    due_at             timestamptz,
    due_hint           text,       -- 'after_office','morning','before_leaving'
    calendar_event_id  uuid REFERENCES calendar_events(id) ON DELETE SET NULL,
    source             text NOT NULL DEFAULT 'explicit'
                         CHECK (source IN ('explicit','extracted','email','system')),
    source_message_id  uuid REFERENCES messages(id) ON DELETE SET NULL,
    source_email_id    uuid,       -- FK added after emails table
    created_at         timestamptz NOT NULL DEFAULT now(),
    completed_at       timestamptz
);
CREATE INDEX IF NOT EXISTS tasks_open_due_idx ON tasks (user_id, due_at) WHERE status = 'open';

-- Recurring habits: workout, pack lunch, carry charger/AirPods/keys
CREATE TABLE IF NOT EXISTS routines (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title        text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('workout','meal_prep','carry_item','habit','other')),
    days_of_week smallint[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
    time_of_day  time,
    anchor       text CHECK (anchor IN ('wake','before_office','after_office','bedtime')),
    anchor_offset_min int DEFAULT 0,
    active       boolean NOT NULL DEFAULT true
);

-- Every notification the agent will ever fire. The scheduler polls this.
CREATE TABLE IF NOT EXISTS reminders (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id      uuid REFERENCES tasks(id) ON DELETE CASCADE,
    routine_id   uuid REFERENCES routines(id) ON DELETE CASCADE,
    event_id     uuid REFERENCES calendar_events(id) ON DELETE CASCADE,
    message      text NOT NULL,
    fire_at      timestamptz NOT NULL,
    channel      text NOT NULL DEFAULT 'push' CHECK (channel IN ('push','voice','in_app')),
    status       text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','sent','acked','snoozed','cancelled','failed')),
    attempts     int NOT NULL DEFAULT 0,
    sent_at      timestamptz,
    dedupe_key   text,
    UNIQUE (user_id, dedupe_key)   -- makes generators idempotent
);
CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders (fire_at) WHERE status = 'pending';

-- Precomputed morning / evening / email digests (read fast, no LLM at request time)
CREATE TABLE IF NOT EXISTS briefings (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('morning','evening','email_digest','nutrition')),
    for_date      date NOT NULL,
    content       text NOT NULL,
    structured    jsonb,
    generated_at  timestamptz NOT NULL DEFAULT now(),
    delivered_at  timestamptz,
    UNIQUE (user_id, kind, for_date)
);

-- ---------------------------------------------------------------------
-- 5. GMAIL
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS emails (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gmail_message_id  text NOT NULL,
    thread_id         text NOT NULL,
    from_addr         text NOT NULL,
    to_addrs          text[],
    subject           text,
    snippet           text,
    body_text         text,
    received_at       timestamptz NOT NULL,
    labels            text[],
    category          text,            -- 'work','finance','travel','promo','personal'
    importance        smallint CHECK (importance BETWEEN 1 AND 5),
    summary           text,
    needs_reply       boolean NOT NULL DEFAULT false,
    status            text NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new','summarized','actioned','ignored')),
    embedding         vector(768),
    UNIQUE (user_id, gmail_message_id)
);
CREATE INDEX IF NOT EXISTS emails_inbox_idx ON emails (user_id, received_at DESC);
CREATE INDEX IF NOT EXISTS emails_embedding_idx ON emails USING hnsw (embedding vector_cosine_ops);

DO $$ BEGIN
    ALTER TABLE tasks ADD CONSTRAINT tasks_source_email_fk
        FOREIGN KEY (source_email_id) REFERENCES emails(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Human-in-the-loop send: nothing is sent unless status = 'approved'
CREATE TABLE IF NOT EXISTS email_drafts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    in_reply_to     uuid REFERENCES emails(id) ON DELETE SET NULL,
    thread_id       text,
    gmail_draft_id  text,
    to_addrs        text[] NOT NULL,
    cc_addrs        text[],
    subject         text NOT NULL,
    body            text NOT NULL,
    status          text NOT NULL DEFAULT 'pending_approval'
                      CHECK (status IN ('pending_approval','approved','rejected','sent','failed')),
    approved_at     timestamptz,
    sent_at         timestamptz,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 6. NUTRITION
-- ---------------------------------------------------------------------
-- Versioned goals so past days are judged against the goal at that time
CREATE TABLE IF NOT EXISTS nutrition_goals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    effective_from  date NOT NULL,
    calories        int NOT NULL,
    protein_g       numeric(6,1),
    carbs_g         numeric(6,1),
    fat_g           numeric(6,1),
    fiber_g         numeric(6,1),
    water_ml        int,
    note            text,
    UNIQUE (user_id, effective_from)
);

-- Reusable food lookup: seed with Indian foods, grows from user corrections
CREATE TABLE IF NOT EXISTS food_catalog (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid REFERENCES users(id) ON DELETE CASCADE,  -- NULL = global
    name           text NOT NULL,
    aliases        text[] NOT NULL DEFAULT '{}',
    serving_desc   text NOT NULL,                -- '1 medium egg', '1 idli'
    serving_grams  numeric(7,1),
    calories       numeric(7,1) NOT NULL,
    protein_g      numeric(6,1) NOT NULL DEFAULT 0,
    carbs_g        numeric(6,1) NOT NULL DEFAULT 0,
    fat_g          numeric(6,1) NOT NULL DEFAULT 0,
    fiber_g        numeric(6,1) NOT NULL DEFAULT 0,
    verified       boolean NOT NULL DEFAULT false,
    embedding      vector(768)
);
CREATE INDEX IF NOT EXISTS food_name_trgm_idx ON food_catalog USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS food_embedding_idx ON food_catalog USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS meals (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    eaten_at         timestamptz NOT NULL DEFAULT now(),
    meal_type        text CHECK (meal_type IN ('breakfast','lunch','dinner','snack')),
    raw_text         text,                        -- what the user said
    photo_url        text,
    source           text NOT NULL DEFAULT 'text' CHECK (source IN ('text','photo','manual')),
    user_corrected   boolean NOT NULL DEFAULT false,
    source_message_id uuid REFERENCES messages(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS meals_user_time_idx ON meals (user_id, eaten_at);

CREATE TABLE IF NOT EXISTS meal_items (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    meal_id       uuid NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    food_id       uuid REFERENCES food_catalog(id),
    name          text NOT NULL,
    quantity      numeric(7,2) NOT NULL DEFAULT 1,
    unit          text,
    calories      numeric(7,1) NOT NULL,
    protein_g     numeric(6,1) NOT NULL DEFAULT 0,
    carbs_g       numeric(6,1) NOT NULL DEFAULT 0,
    fat_g         numeric(6,1) NOT NULL DEFAULT 0,
    fiber_g       numeric(6,1) NOT NULL DEFAULT 0,
    confidence    real                            -- LLM estimate confidence
);

CREATE OR REPLACE VIEW daily_nutrition AS
SELECT m.user_id,
       (m.eaten_at AT TIME ZONE u.timezone)::date AS day,
       sum(i.calories)  AS calories,
       sum(i.protein_g) AS protein_g,
       sum(i.carbs_g)   AS carbs_g,
       sum(i.fat_g)     AS fat_g,
       sum(i.fiber_g)   AS fiber_g
FROM meals m
JOIN users u       ON u.id = m.user_id
JOIN meal_items i  ON i.meal_id = m.id
GROUP BY 1, 2;

-- ---------------------------------------------------------------------
-- 7. OBSERVABILITY
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id     uuid REFERENCES messages(id) ON DELETE SET NULL,
    intent         text,
    tools_called   text[],
    retrieval_ms   int,
    llm_ms         int,
    total_ms       int,
    tokens_in      int,
    tokens_out     int,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
-- ADDITIONS (required by ARCHITECTURE_PLAN.txt but missing in Tables.txt)
-- =====================================================================

-- Persona is data, not code (Architecture section 5)
CREATE TABLE IF NOT EXISTS personas (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL UNIQUE,
    aliases       text[] NOT NULL DEFAULT '{}',
    gender        text,
    tone          text,
    system_prompt text NOT NULL,
    speaking_style text,
    greeting      text,
    tts_voice_id  text,
    tts_style     text,
    tts_rate      int,
    tts_pitch     int,
    is_default    boolean NOT NULL DEFAULT false,
    active        boolean NOT NULL DEFAULT true
);

ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS active_persona_id uuid REFERENCES personas(id);

-- In-app inbox: where reminders/briefings land on the PC (replaces FCM for now).
CREATE TABLE IF NOT EXISTS notifications (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        text NOT NULL DEFAULT 'reminder',   -- reminder|briefing|alert|email
    title       text NOT NULL,
    body        text,
    reminder_id uuid REFERENCES reminders(id) ON DELETE SET NULL,
    urgent      boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    seen_at     timestamptz
);
CREATE INDEX IF NOT EXISTS notifications_unseen_idx ON notifications (user_id, created_at DESC) WHERE seen_at IS NULL;

-- Every write the agent makes on its own is recorded here (no deletes exist anywhere).
CREATE TABLE IF NOT EXISTS audit_log (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    actor       text NOT NULL DEFAULT 'agent',      -- agent|extractor|job|api
    action      text NOT NULL,                      -- insert|update|status_change
    table_name  text NOT NULL,
    row_id      text,
    before      jsonb,
    after       jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_time_idx ON audit_log (user_id, created_at DESC);

-- Free-form Gmail sync state is kept on oauth_credentials; extra helper indexes:
CREATE INDEX IF NOT EXISTS tasks_title_trgm_idx ON tasks USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS messages_user_time_idx ON messages (user_id, created_at DESC);
