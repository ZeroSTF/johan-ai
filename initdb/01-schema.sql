CREATE TABLE IF NOT EXISTS jobs (
    id          SERIAL PRIMARY KEY,
    hash        TEXT UNIQUE NOT NULL,
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    company     TEXT,
    title       TEXT,
    location    TEXT,
    raw_desc    TEXT,
    scraped_at  TIMESTAMPTZ DEFAULT now(),
    parsed      JSONB,
    match_score REAL,
    status      TEXT DEFAULT 'new',
    last_seen   TIMESTAMPTZ DEFAULT now(),   -- refreshed every scrape that still lists it
    closed_at   TIMESTAMPTZ                  -- set when the posting is gone (see johan_refresh.py)
);
CREATE INDEX IF NOT EXISTS jobs_open_idx ON jobs (source, company) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS applications (
    id           SERIAL PRIMARY KEY,
    job_id       INT REFERENCES jobs(id) ON DELETE CASCADE,
    cv_path      TEXT,
    cl_path      TEXT,
    bullets      TEXT[],
    verify_log   JSONB,
    created_at   TIMESTAMPTZ DEFAULT now(),
    submitted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_score_idx  ON jobs (match_score DESC NULLS LAST);

-- Added 2026-09-10 for the assistant's own memory and task list. These are
-- Johan's, not the job pipeline's: things Riadh explicitly asked it to
-- remember, and a durable todo list. Semantic recall lives in the Qdrant
-- collection `johan_memory`; Postgres is the source of truth for the text.
CREATE TABLE IF NOT EXISTS memories (
    id         SERIAL PRIMARY KEY,
    text       TEXT NOT NULL,
    kind       TEXT DEFAULT 'fact',   -- fact | preference | personal | project
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    id         SERIAL PRIMARY KEY,
    text       TEXT NOT NULL,
    done       BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now(),
    done_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS tasks_done_idx ON tasks (done, created_at DESC);

-- Added 2026-09-13. Every tool invocation, so reliability is measured rather
-- than guessed: which tools the model reaches for, which arguments it gets
-- wrong, how long each takes. This is the data an eval harness scores and the
-- only honest way to judge a model or prompt change.
CREATE TABLE IF NOT EXISTS tool_calls (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ DEFAULT now(),
    session_id   TEXT,
    model        TEXT,
    user_message TEXT,
    hop          INT,
    tool         TEXT NOT NULL,
    args         JSONB,
    ok           BOOLEAN,
    error        TEXT,
    duration_ms  INT,
    result_chars INT,
    eval_run     TEXT          -- set when the call came from the eval harness
);

CREATE INDEX IF NOT EXISTS tool_calls_ts_idx   ON tool_calls (ts DESC);
CREATE INDEX IF NOT EXISTS tool_calls_tool_idx ON tool_calls (tool, ok);
CREATE INDEX IF NOT EXISTS tool_calls_eval_idx ON tool_calls (eval_run) WHERE eval_run IS NOT NULL;

-- Every scored decision from johan_decide.py. Low-confidence rows are the
-- raw material for self-improvement: they show where Johan was unsure.
CREATE TABLE IF NOT EXISTS decisions (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ DEFAULT now(),
    session_id   TEXT,
    model        TEXT,
    kind         TEXT,          -- route / step_done / supported / risky / ...
    question     TEXT,
    options      JSONB,         -- option keys, in the order offered
    probs        JSONB,         -- key -> probability
    choice       TEXT,
    p_top        REAL,
    margin       REAL,
    coverage     REAL,          -- mass the model put on ANY valid letter
    confident    BOOLEAN,
    duration_ms  INT,
    eval_run     TEXT,
    context      TEXT,          -- what the decision judged (truncated)
    detail       JSONB          -- e.g. a rewrite's draft and corrected answer
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions (ts DESC);
CREATE INDEX IF NOT EXISTS decisions_unsure ON decisions (kind) WHERE NOT confident;
