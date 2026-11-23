-- PuppyGraph PoC: read-only graph contract over the canonical PostgreSQL tables.
--
-- PostgreSQL stays the source of truth (entities / relationships / provenance). PuppyGraph only
-- supports scalar column types (STRING, INT, LONG, DOUBLE, BOOLEAN, DATE, DATETIME), so these views
-- flatten the JSON identity into columns and aggregate provenance into reference strings.
-- PuppyGraph queries them live (no copy), so ingestion updates are visible on the next query.

CREATE SCHEMA IF NOT EXISTS graph;

CREATE OR REPLACE VIEW graph.entity_v AS
SELECT
    e.id,
    e.tenant_id,
    e.type,
    e.canonical_key,
    e.name,
    e.description,
    e.status,
    e.version,
    COALESCE(e.customer_scope, '')                         AS customer,          -- '' = shared entity
    COALESCE(e.identity ->> 'tier', e.properties ->> 'tier', '') AS environment_tier,
    COALESCE(e.identity ->> 'application', '')             AS application_name,
    COALESCE(e.properties ->> 'url', '')                    AS url,
    COALESCE(e.properties ->> 'namespace', '')              AS namespace,
    COALESCE(e.properties ->> 'resource_type', '')          AS resource_type,
    COALESCE(p.sources, '')                                 AS source_systems,
    COALESCE(p.source_refs, '')                             AS source_refs,
    e.updated_at
FROM entities e
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT pr.source_system, ',')       AS sources,
           string_agg(DISTINCT pr.source_record_key, ',')   AS source_refs
    FROM provenance pr
    WHERE pr.tenant_id = e.tenant_id AND pr.subject_kind = 'entity' AND pr.subject_id = e.id
) p ON TRUE;

CREATE OR REPLACE VIEW graph.relationship_v AS
SELECT
    r.id,
    r.tenant_id,
    r.type,
    r.source_id,
    r.target_id,
    s.type                                                  AS source_type,
    t.type                                                  AS target_type,
    r.confidence,
    r.status,
    r.version,
    -- An edge is customer-scoped if either endpoint is (used for access-control filtering).
    COALESCE(NULLIF(s.customer_scope, ''), NULLIF(t.customer_scope, ''), '') AS customer,
    COALESCE(p.sources, '')                                 AS source_systems,
    COALESCE(p.urls, '')                                    AS source_urls,
    COALESCE(p.evidence, '')                                AS evidence
FROM relationships r
JOIN entities s ON s.id = r.source_id
JOIN entities t ON t.id = r.target_id
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT pr.source_system, ',')       AS sources,
           string_agg(DISTINCT pr.source_url, ' ')          AS urls,
           left(string_agg(DISTINCT pr.evidence, ' | '), 1000) AS evidence
    FROM provenance pr
    WHERE pr.tenant_id = r.tenant_id AND pr.subject_kind = 'relationship' AND pr.subject_id = r.id
) p ON TRUE;

-- Dedicated least-privilege login for PuppyGraph: read-only, graph schema only.
-- The password comes from the psql variable ro_password (setup_poc.py passes $PUPPYGRAPH_PG_PASSWORD):
--   psql -v ro_password=... -f views.sql
SELECT format('CREATE ROLE puppygraph_ro LOGIN PASSWORD %L', :'ro_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'puppygraph_ro') \gexec
SELECT format('ALTER ROLE puppygraph_ro WITH LOGIN PASSWORD %L', :'ro_password') \gexec
GRANT USAGE ON SCHEMA graph TO puppygraph_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA graph TO puppygraph_ro;
-- Views run with the owner's rights, so the role needs no access to the base tables.
