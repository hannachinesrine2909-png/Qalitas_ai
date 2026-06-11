CREATE OR REPLACE VIEW v_a1_error_memory_stats AS
SELECT
    tenant_id,
    error_family,
    req_type,
    fix_rule,
    memory_action,
    severity,
    COUNT(*) AS patterns_count,
    SUM(hit_count) AS hit_count_total,
    SUM(cardinality(doc_ids)) AS docs_count_total,
    SUM(cardinality(source_event_keys)) AS backfilled_events_total,
    MAX(last_seen_at) AS last_seen_at,
    MAX(last_replayed_at) AS last_replayed_at,
    SUM(replay_count) AS replay_count_total
FROM a1_error_memory
GROUP BY tenant_id, error_family, req_type, fix_rule, memory_action, severity;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qalitas_app') THEN
        GRANT SELECT ON v_a1_error_memory_stats TO qalitas_app;
    END IF;
END
$$;
