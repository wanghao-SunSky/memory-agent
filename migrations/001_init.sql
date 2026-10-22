-- 001_init: 第 1 周最小可演示版的全部表
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE spaces (
    id           BIGSERIAL PRIMARY KEY,
    owner_id     TEXT NOT NULL,
    name         TEXT NOT NULL,              -- work / life
    display_name TEXT NOT NULL,              -- 工作 / 生活
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name)
);

-- 原始对话：只追加；删除时 content 置 NULL、excluded=true，只留 content_hash
CREATE TABLE episodes (
    id           BIGSERIAL PRIMARY KEY,
    space_id     BIGINT NOT NULL REFERENCES spaces(id),
    session_id   TEXT NOT NULL,
    turn_idx     INT,
    role         TEXT NOT NULL CHECK (role IN ('user','assistant','tool')),
    source_type  TEXT NOT NULL CHECK (source_type IN ('user_utterance','assistant','tool_result','web','document')),
    trust        TEXT NOT NULL CHECK (trust IN ('trusted','untrusted')),
    content      TEXT,
    occurred_at  TIMESTAMPTZ NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash TEXT NOT NULL,
    redacted     BOOLEAN NOT NULL DEFAULT false,
    excluded     BOOLEAN NOT NULL DEFAULT false,
    excluded_at  TIMESTAMPTZ,
    UNIQUE (space_id, content_hash)
);
CREATE INDEX episodes_session_idx ON episodes (space_id, session_id, occurred_at);

-- 事实：双时间轴（valid_* 事件时间轴；recorded_at/expired_at 记录时间轴）
CREATE TABLE facts (
    id                 BIGSERIAL PRIMARY KEY,
    space_id           BIGINT NOT NULL REFERENCES spaces(id),
    lineage_id         BIGINT NOT NULL,        -- 同一事实的版本链 id（= 第一个版本的 id）
    subject            TEXT NOT NULL,
    predicate          TEXT NOT NULL,
    object_text        TEXT NOT NULL,
    fact_text          TEXT NOT NULL,
    fact_key           TEXT NOT NULL,          -- 归一化 subject|predicate
    time_expression    TEXT,                   -- 原始时间表达（换算失败时保留原文）
    valid_from         TIMESTAMPTZ NOT NULL,
    valid_to           TIMESTAMPTZ,
    recorded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expired_at         TIMESTAMPTZ,
    last_confirmed_at  TIMESTAMPTZ,
    status             TEXT NOT NULL CHECK (status IN ('active','superseded','retracted','deleted')),
    confidence         REAL,
    source_episode_ids BIGINT[] NOT NULL CHECK (cardinality(source_episode_ids) > 0),
    origin_episode_id  BIGINT NOT NULL,
    dedupe_hash        TEXT NOT NULL,          -- sha256(fact_key|object_text)
    supersedes_id      BIGINT REFERENCES facts(id) ON DELETE SET NULL,
    created_by         TEXT NOT NULL CHECK (created_by IN ('hot_path','consolidation','user_edit','worker')),
    version            INT NOT NULL DEFAULT 1,
    UNIQUE (space_id, origin_episode_id, dedupe_hash)   -- 事实写入幂等
);
CREATE INDEX facts_space_status_idx ON facts (space_id, status);
CREATE INDEX facts_key_idx ON facts (space_id, fact_key) WHERE status = 'active';
CREATE INDEX facts_lineage_idx ON facts (lineage_id);

CREATE TABLE retrieval_keys (
    id          BIGSERIAL PRIMARY KEY,
    space_id    BIGINT NOT NULL REFERENCES spaces(id),
    owner_type  TEXT NOT NULL CHECK (owner_type IN ('fact','episode_round')),
    owner_id    BIGINT NOT NULL,
    key_text    TEXT NOT NULL,
    embedding   vector(512) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_type, owner_id)
);
CREATE INDEX retrieval_keys_space_idx ON retrieval_keys (space_id, owner_type);
CREATE INDEX retrieval_keys_hnsw ON retrieval_keys USING hnsw (embedding vector_cosine_ops);

CREATE TABLE memory_ops (
    id             BIGSERIAL PRIMARY KEY,
    space_id       BIGINT NOT NULL REFERENCES spaces(id),
    op             TEXT NOT NULL,      -- ADD / INVALIDATE / NOOP / REDACT / DELETE
    fact_id        BIGINT,             -- 不加外键：删除后审计行保留（正文会被擦除）
    episode_id     BIGINT,
    before         JSONB,
    after          JSONB,
    reason         TEXT,
    model          TEXT,
    prompt_version TEXT,
    trace_id       TEXT,
    actor          TEXT NOT NULL CHECK (actor IN ('system','user')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX memory_ops_fact_idx ON memory_ops (fact_id);

CREATE TABLE jobs (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('extract','consolidate','reindex','purge')),
    payload         JSONB NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','dead','cancelled')),
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 5,
    run_after       TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by       TEXT,
    locked_until    TIMESTAMPTZ,
    last_error      TEXT,
    trace_id        TEXT,
    result          JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX jobs_claim_idx ON jobs (status, run_after);

-- 删除凭证：不存被删内容，只存哈希和验证结果
CREATE TABLE deletion_log (
    id               BIGSERIAL PRIMARY KEY,
    space_id         BIGINT NOT NULL REFERENCES spaces(id),
    target_memory_id BIGINT NOT NULL,
    scope            JSONB NOT NULL,
    content_hashes   TEXT[] NOT NULL,
    deleted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    result           TEXT NOT NULL CHECK (result IN ('pending','verified','failed')),
    verification     JSONB,
    verified_at      TIMESTAMPTZ
);
