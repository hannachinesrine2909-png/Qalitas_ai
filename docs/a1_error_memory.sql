CREATE TABLE IF NOT EXISTS a1_error_memory (
    memory_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    error_family TEXT NOT NULL,
    req_type TEXT NOT NULL DEFAULT 'AUTRE',
    trigger_pattern TEXT NOT NULL,
    signature_pattern TEXT NOT NULL DEFAULT '',
    bad_output TEXT NOT NULL DEFAULT '',
    snippet_preview TEXT NOT NULL DEFAULT '',
    fix_rule TEXT NOT NULL DEFAULT '',
    prompt_patch TEXT NOT NULL DEFAULT '',
    memory_action TEXT NOT NULL DEFAULT 'OBSERVE_ONLY',
    severity TEXT NOT NULL DEFAULT 'MEDIUM',
    filter_name TEXT NOT NULL DEFAULT '',
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    article_label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    sample_doc_id TEXT NOT NULL DEFAULT '',
    doc_ids TEXT[] NOT NULL DEFAULT '{}',
    hit_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_replayed_at TIMESTAMPTZ NULL,
    replay_count INTEGER NOT NULL DEFAULT 0,
    replay_notes TEXT NOT NULL DEFAULT '',
    source_event_type TEXT NOT NULL DEFAULT 'A1_ERROR_MEMORY_SIGNAL',
    source_event_keys TEXT[] NOT NULL DEFAULT '{}',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT a1_error_memory_uq UNIQUE (tenant_id, error_family, trigger_pattern)
);

ALTER TABLE a1_error_memory
ADD COLUMN IF NOT EXISTS source_event_keys TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_a1_error_memory_tenant_last_seen
ON a1_error_memory(tenant_id, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_a1_error_memory_tenant_signature
ON a1_error_memory(tenant_id, signature_pattern);

CREATE INDEX IF NOT EXISTS idx_a1_error_memory_tenant_doc
ON a1_error_memory(tenant_id, sample_doc_id);

ALTER TABLE a1_error_memory ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_a1_error_memory_tenant ON a1_error_memory;
CREATE POLICY p_a1_error_memory_tenant ON a1_error_memory
    USING (lower(coalesce(tenant_id, '')) = lower(coalesce(current_setting('app.tenant_id', true), '')))
    WITH CHECK (lower(coalesce(tenant_id, '')) = lower(coalesce(current_setting('app.tenant_id', true), '')));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qalitas_app') THEN
        GRANT SELECT, INSERT, UPDATE ON a1_error_memory TO qalitas_app;
        GRANT USAGE, SELECT ON SEQUENCE a1_error_memory_memory_id_seq TO qalitas_app;
    END IF;
END
$$;
