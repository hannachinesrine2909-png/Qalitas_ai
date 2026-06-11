-- Vue de pilotage des rejets d'exigences par Agent 1
-- A executer une seule fois sur la base Qalitas_ai (idempotent avec OR REPLACE)
--
-- La colonne JSONB de la table events est "payload" (verifier avec : \d events)
--
-- Utilisation :
--   SELECT * FROM v_rejection_stats WHERE tenant_id = '<tenant_id>' ORDER BY total DESC;
--   SELECT * FROM v_rejection_stats WHERE doc_id = '<uuid>' ORDER BY total DESC;

CREATE OR REPLACE VIEW v_rejection_stats AS
SELECT
    e.tenant_id,
    e.doc_id,
    e.payload->>'filter'       AS filter_name,
    e.payload->>'req_type'     AS req_type,
    e.payload->>'article_label' AS article_label,
    COUNT(*)                                                               AS total,
    MAX(e.created_at)                                                      AS last_seen
FROM events e
WHERE e.event_type = 'REQUIREMENT_REJECTED'
GROUP BY
    e.tenant_id,
    e.doc_id,
    e.payload->>'filter',
    e.payload->>'req_type',
    e.payload->>'article_label'
ORDER BY total DESC;

-- Vue agregee par filtre uniquement (tableau de bord global)
CREATE OR REPLACE VIEW v_rejection_stats_global AS
SELECT
    e.tenant_id,
    e.payload->>'filter'   AS filter_name,
    e.payload->>'req_type' AS req_type,
    COUNT(*)                    AS total,
    COUNT(DISTINCT e.doc_id)    AS docs_affected,
    MAX(e.created_at)           AS last_seen
FROM events e
WHERE e.event_type = 'REQUIREMENT_REJECTED'
GROUP BY
    e.tenant_id,
    e.payload->>'filter',
    e.payload->>'req_type'
ORDER BY total DESC;

-- Vue des articles avec renvois inter-articles detectes
CREATE OR REPLACE VIEW v_cross_reference_articles AS
SELECT
    e.tenant_id,
    e.doc_id,
    e.payload->>'article_id'    AS article_id,
    e.payload->>'article_label' AS article_label,
    e.payload->>'note'          AS note,
    e.created_at
FROM events e
WHERE e.event_type = 'ARTICLE_HAS_CROSS_REFERENCE'
ORDER BY e.created_at DESC;
