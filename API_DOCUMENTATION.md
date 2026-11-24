# REST API Reference

The REST API is a FastAPI application (`src/ontology_platform/api/app.py`). The live OpenAPI schema
is served by the running API:

| URL | Content |
|---|---|
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/openapi.json` | OpenAPI document |

Base URL: `http://127.0.0.1:8000` (`ontology-platform serve` default, and the port Podman Compose
publishes). All data endpoints are under `/api/v1`.

## 1. Authentication

Every `/api/v1` endpoint requires a credential:

```
Authorization: Bearer <api key or JWT>
```

The `X-API-Key: <api key or JWT>` header is also accepted. It is used only when `Authorization` is
absent. API keys have the form `op_<key id>_<secret>`. JWT verification is enabled only when
`OP_JWT_KEY` is set. Roles, permissions and the JWT claims are described in [SECURITY.md](SECURITY.md).

The caller's tenant always comes from the credential, never from the request. Customer-restricted
credentials see only their customers' data (other customers' entities return `404`).

Unauthenticated endpoints: `/health`, `/ready`, `/metrics`, `/docs`, `/redoc`, `/openapi.json` and the
static console under `/ui`.

## 2. Conventions

**Request ID.** Every response carries `X-Request-ID`. If the request sends `X-Request-ID`, the same
value is echoed; otherwise a random hex id is generated. The id is the `correlation_id` in the JSON
logs. A workflow run started by the request records it as the run's `correlation_id`.

**Errors.** Platform errors use one JSON shape:

```json
{"error": "not_found", "message": "no entity matches 'nothing-here'"}
```

| `error` | HTTP status | Typical cause |
|---|---|---|
| `unauthenticated` | 401 | Missing, malformed, revoked or invalid credential (response also carries `WWW-Authenticate: Bearer`) |
| `forbidden` | 403 | The role lacks the endpoint's permission; a customer-restricted admin asked for a broader key |
| `not_found` | 404 | Unknown entity, run, connector or key, or one outside the caller's tenant or customer scope |
| `conflict` | 409 | Duplicate connector name; connector disabled; ingestion already running for the connector |
| `validation_error` | 422 | Semantic validation in the platform (invalid connector config, `secret_ref` not a reference, unknown role or connector type, depth < 1) |
| `connector_error`, `connector_transient_error` | 502 | A source system failed during a synchronous operation |
| `platform_error` | 500 | Other platform errors |

Request-schema violations (missing field, value out of range, unknown enum value) are rejected by
FastAPI before the platform runs. They return `422` in FastAPI's format
(`{"detail": [{"loc": ..., "msg": ..., "type": ...}]}`), not the shape above. Unhandled exceptions
return a plain `500 Internal Server Error`.

**Entity references.** Wherever a path or parameter takes an entity (`{ref}`, `source`, `target`,
`application`, `entity`), it accepts an entity id, a canonical key (`application:payments-platform`)
or a name/alias. An unknown reference returns `404`; an ambiguous name returns `422` listing the
candidates (pass a canonical key or id instead).

**Limits.** Traversal depth parameters accept 1–12, but the engine caps them at
`OP_MAX_TRAVERSAL_DEPTH` (default 6). Entity search results are capped at `OP_MAX_RESULTS`
(default 200).

**Pagination.** Only `GET /api/v1/agent/runs` pages with `limit` + `offset` and returns `total`.
Other list endpoints take a `limit` and return the newest or best-ranked items first.

## 3. Endpoint summary

| Method | Path | Permission | Success |
|---|---|---|---|
| GET | `/health` | none | 200 |
| GET | `/ready` | none | 200 |
| GET | `/metrics` | none | 200 |
| GET | `/api/v1/ontology/schema` | `graph:read` | 200 |
| GET | `/api/v1/graph/stats` | `graph:read` | 200 |
| GET | `/api/v1/entities` | `graph:read` | 200 |
| GET | `/api/v1/entities/{ref}` | `graph:read` | 200 |
| GET | `/api/v1/entities/{ref}/related` | `graph:read` | 200 |
| GET | `/api/v1/paths` | `graph:read` | 200 |
| GET | `/api/v1/dependencies/{ref}` | `graph:read` | 200 |
| GET | `/api/v1/deployments` | `graph:read` | 200 |
| GET | `/api/v1/documents/search` | `graph:read` | 200 |
| POST | `/api/v1/query` | `graph:read` | 200 |
| POST | `/api/v1/agent/query` | `graph:read` | 200 |
| GET | `/api/v1/agent/runs` | `graph:read` | 200 |
| GET | `/api/v1/agent/runs/{run_id}` | `graph:read` | 200 |
| GET | `/api/v1/agent/runs/{run_id}/events` | `graph:read` | 200 |
| GET | `/api/v1/connector-types` | `connectors:read` | 200 |
| GET | `/api/v1/connectors` | `connectors:read` | 200 |
| POST | `/api/v1/connectors` | `connectors:write` | 201 |
| POST | `/api/v1/connectors/{connector_id}/sync` | `ingest:run` | 202 |
| POST | `/api/v1/connectors/{connector_id}/records` | `ingest:run` | 200 |
| GET | `/api/v1/ingestion/runs` | `connectors:read` | 200 |
| GET | `/api/v1/ingestion/runs/{run_id}` | `connectors:read` | 200 |
| GET | `/api/v1/admin/api-keys` | `keys:manage` | 200 |
| POST | `/api/v1/admin/api-keys` | `keys:manage` | 201 |
| DELETE | `/api/v1/admin/api-keys/{key_id}` | `keys:manage` | 204 |
| GET | `/api/v1/admin/audit` | `audit:read` | 200 |
| GET | `/ui`, `/ui/...` | none (static files) | 307 / 200 |

Every authenticated endpoint can also return `401`, and `403` when the permission is missing.

## 4. Operations

| Endpoint | Response |
|---|---|
| `GET /health` | `{"status": "ok"}`. Liveness only; does not touch the database. |
| `GET /ready` | `{"status": "ready"}` after a `SELECT 1` succeeds. If the database is unreachable the request fails with `500`. |
| `GET /metrics` | Prometheus text format. Metric names are listed in [DEPLOYMENT.md](DEPLOYMENT.md#4-health-readiness-and-metrics). |

## 5. Ontology and graph

### `GET /api/v1/ontology/schema`
Returns `entity_types` (type, description, identity fields, customer scoping) and
`relationship_types` (type, description, allowed source and target types). The schema itself is
documented in [ONTOLOGY_SCHEMA.md](ONTOLOGY_SCHEMA.md).

### `GET /api/v1/graph/stats`
`{"entities": {type: count}, "relationships": {type: count}, "revision": n}`. Counts honour the
caller's customer scope. `revision` is the tenant's graph revision counter.

### `GET /api/v1/entities`
| Parameter | Type | Default | Notes |
|---|---|---|---|
| `q` | string ≤ 500 | `""` | Name, alias or keywords. Empty lists entities. |
| `type` | entity type, repeatable | all | e.g. `type=service&type=api` |
| `limit` | 1–500 | 20 | Capped at `OP_MAX_RESULTS` |

Response: `{"results": [{"id", "type", "name", "key", "status", "description", "score", "match"}]}`.
Exact name/alias matches come first (`match: "exact"`), then BM25 matches (`match: "lexical"`).

### `GET /api/v1/entities/{ref}`
| Parameter | Default | Notes |
|---|---|---|
| `history` | `false` | Include version history snapshots |

Response: `entity` (id, type, name, key, status, description, identity, properties, customer_scope,
version, updated_at), `aliases`, `relationships` (relationship_id, type, direction, confidence,
`other` entity; at most 100), `relationship_count`, `provenance` (source_system, source_url,
extractor, confidence, evidence, observed_at) and, with `history=true`, `history` (version, change,
at, run_id, snapshot). `404` if the entity is not visible to the caller.

### `GET /api/v1/entities/{ref}/related`
| Parameter | Values | Default |
|---|---|---|
| `relation` | relationship type, repeatable | all |
| `direction` | `out`, `in`, `both` | `both` |
| `target_type` | entity type, repeatable | all |
| `depth` | 1–12 (capped at `OP_MAX_TRAVERSAL_DEPTH`) | 1 |

Response: `{"root", "count", "related": [{"entity", "depth", "path"}]}`. Paths here carry no
provenance; use `/paths` or `/entities/{ref}` for evidence.

### `GET /api/v1/paths`
| Parameter | Required | Default |
|---|---|---|
| `source` | yes | |
| `target` | yes | |
| `max_depth` | no | 4 (1–12, capped at `OP_MAX_TRAVERSAL_DEPTH`) |

Response: `{"source", "target", "connected", "paths"}`. Up to 5 shortest paths. Each path is a list of
edges: `relationship_id`, `type`, `source`, `target`, `source_id`, `target_id`, `confidence` and up
to 3 `evidence` items (provenance).

## 6. Query

### `GET /api/v1/dependencies/{ref}`
| Parameter | Values | Default |
|---|---|---|
| `direction` | `downstream` (what it depends on), `upstream` (what depends on it) | `downstream` |
| `depth` | 1–12 (capped at `OP_MAX_TRAVERSAL_DEPTH`) | 4 |

Response: `root`, `direction`, `items` (`entity`, `hops`, `path` with evidence). For an application
root, `components` lists its services. With `direction=upstream`, `impact` adds
`affected_services`, `affected_applications`, `affected_deployments` and `affected_customers`.

### `GET /api/v1/deployments`
| Parameter | Required | Notes |
|---|---|---|
| `application` | yes | Entity reference |
| `customer` | no | Customer name or alias |
| `environment` | no | `dev`, `sit`, `uat`, `staging`, `prod` (aliases such as `production` accepted) |

Response: `application`, `filters`, and `deployments`: per deployment the `customer`,
`environment_tier`, `environment`, `clusters`, `namespace`, `workloads`, `cloud_resources`, `sources`
and up to 3 `evidence` items.

### `GET /api/v1/documents/search`
| Parameter | Required | Default |
|---|---|---|
| `q` | yes (1–500 chars) | |
| `entity` | no | only documents describing this entity |
| `limit` | no | 5 (1–50) |

Response: `{"query", "about", "results": [{"chunk_id", "document_entity_id", "title", "url", "text", "score"}]}`.

### `POST /api/v1/query`
Single-intent question answering (the same engine as the `ask` CLI command and the
`query_knowledge_graph` MCP tool).

```json
{"question": "Which Kubernetes clusters host Customer B's applications?", "limit": 50}
```

`question` 3–1000 characters; `limit` 1–500 (default 50). Response: `question`, `intent`,
`linked_entities` (with `matched_text`), a one-line `answer`, `results` with evidence paths, and
intent-specific fields.

## 7. Agent workflow

The multi-agent workflow (classification, planning, specialist agents, evidence validation,
synthesis) is described in [AGENTIC_WORKFLOW.md](AGENTIC_WORKFLOW.md). Runs are stored per tenant.

### `POST /api/v1/agent/query`
```json
{"question": "Which applications depend on Payment Service, and where are they deployed?"}
```

`question` is 3–1000 characters. The request waits for the whole run and returns it with `200`, even
when the run's own `status` is `completed_with_errors` or `failed`. Response fields:

| Field | Content |
|---|---|
| `id`, `tenant_id`, `principal`, `correlation_id`, `query` | Run identity |
| `status` | `completed`, `completed_with_errors`, `failed` (`running` and `cancelled` appear only in stored runs) |
| `intents`, `linked_entities` | Classification result |
| `plan` | Steps: id, agent, action, purpose, inputs, depends_on, status, attempts, timings, tools, error, skip_reason, summary |
| `events` | Timeline (type, step_id, agent, message, data) |
| `facts` | Facts with `statement`, `path`, `evidence`, `status` (`confirmed`, `inferred`, `unsupported`) and `validation_notes` |
| `validation` | `status` (`validated`, `partially_validated`, `insufficient_evidence`, `failed`), counts, `issues` |
| `answer` | `summary`, `sections`, `confirmed_facts`, `inferred_facts`, `missing_information`, `limitations`, `sources` |
| `started_at`, `finished_at`, `duration_ms` | Timing |

### `GET /api/v1/agent/runs`
| Parameter | Range | Default |
|---|---|---|
| `limit` | 1–100 | 20 |
| `offset` | ≥ 0 | 0 |

Response: `{"runs": [{"id", "query", "status", "intents", "principal", "started_at", "duration_ms", "validation_status"}], "total", "limit", "offset"}`,
newest first. Only the latest `OP_WORKFLOW_RUNS_RETAINED` runs per tenant are kept.

### `GET /api/v1/agent/runs/{run_id}`
The stored run document (same shape as `POST /agent/query`). `404` for an unknown run or a run of
another tenant.

### `GET /api/v1/agent/runs/{run_id}/events`
`{"events": [...]}`: the run's event timeline only.

## 8. Connectors and ingestion

### `GET /api/v1/connector-types`
`{"types": [...]}`: `aws`, `azure`, `bitbucket`, `catalog_file`, `confluence`, `fixture`, `gcp`,
`github`, `gitlab`, `kubernetes`, `push`.

### `GET /api/v1/connectors`
`{"connectors": [{"id", "name", "type", "config", "secret_ref", "enabled", "cursor", "created_at"}]}`.
`secret_ref` is the reference (`env:NAME` or `file:/path`), never the secret.

### `POST /api/v1/connectors` → `201`
```json
{"name": "github-main", "type": "github", "config": {"org": "my-org"}, "secret_ref": "env:GITHUB_TOKEN", "enabled": true}
```

`name` matches `^[A-Za-z0-9][A-Za-z0-9 _.-]*$`. `config` is validated against the connector type's
config model. `secret_ref` must be `env:VAR` or `file:/path`, and is required for connector types
that need credentials. Returns the connector. Errors: `422` (unknown type, invalid config or
secret_ref), `409` (name already exists in the tenant).

### `POST /api/v1/connectors/{connector_id}/sync` → `202`
Body: `{"mode": "full"}` or `{"mode": "incremental"}` (default `full`). The run row is created
before the response; ingestion continues as a background task in the API process.

```json
{"run_id": "…", "status": "running"}
```

`409` if the connector is disabled or already has a running ingestion; `404` if the connector is not
in the caller's tenant. Poll `GET /api/v1/ingestion/runs/{run_id}` for the outcome.

### `POST /api/v1/connectors/{connector_id}/records` → `200`
Pushes 1–1000 normalized records (intended for connectors of type `push`, fed by webhooks or CI) and
ingests them within the request.

```json
{"records": [{"kind": "catalog", "external_id": "catalog:cmdb-1", "source_system": "cmdb",
              "payload": {"entities": [{"type": "customer", "name": "umbrella"}], "relationships": []}}]}
```

Record fields: `kind` (`repository`, `document`, `k8s_workload`, `cloud_resource`, `catalog`),
`external_id`, `source_system`, `payload`, optional `source_url` and `updated_at`. Response:
`{"run_id", "status", "error", "stats"}`.

### `GET /api/v1/ingestion/runs`
Parameters: `connector_id` (optional filter), `limit` 1–500 (default 50). Response:
`{"runs": [{"id", "connector_id", "mode", "status", "stats", "error", "triggered_by", "started_at", "finished_at"}]}`,
newest first.

### `GET /api/v1/ingestion/runs/{run_id}`
One run in the same shape. `status` is `running`, `succeeded`, `partial` or `failed`. `stats` holds
the counters (`records_seen`, `records_unchanged`, `records_processed`, `records_failed`,
`records_discovered`, `entities_created`, `entities_updated`, `duplicates_resolved`,
`relationships_created`, `relationships_updated`, `relationships_rejected`, `mentions_linked`,
`stale_entities`, `stale_relationships`), up to 50 `warnings` and `source_errors`, and
`source_error_count`.

## 9. Administration

### `GET /api/v1/admin/api-keys`
`{"api_keys": [{"id", "name", "roles", "allowed_customers", "revoked", "created_at", "last_used_at"}]}`.
Hashes and plaintext keys are never returned.

### `POST /api/v1/admin/api-keys` → `201`
```json
{"name": "ai-assistant", "roles": ["viewer"], "allowed_customers": ["acme"]}
```

`roles` is a non-empty subset of `viewer`, `ingestor`, `admin`. Omit `allowed_customers` (or send
`null`) for access to all customers of the tenant. Response: `id`, `tenant_id`, `name`, `roles`,
`allowed_customers` and `api_key`. The plaintext `api_key` is returned only here. A
customer-restricted caller gets `403` when the requested customers are not a subset of its own.

### `DELETE /api/v1/admin/api-keys/{key_id}` → `204`
Revokes the key. `404` for an unknown key or a key of another tenant.

### `GET /api/v1/admin/audit`
Parameter `limit` 1–1000 (default 100). Response:
`{"entries": [{"at", "principal", "channel", "action", "params", "outcome", "detail"}]}`, newest
first. `params` and `detail` are stored with credentials redacted (see [SECURITY.md](SECURITY.md#6-audit-log)).

## 10. Operator console

`GET /ui` redirects (`307`) to `/ui/`, which serves the static console (HTML, CSS, JavaScript; no
build step). It is a client of the endpoints above and needs an API key pasted into the page. It is
not part of the OpenAPI schema.

## 11. Examples

The examples use a key in a shell variable (`KEY=op_...`; in PowerShell `$key = "op_..."`).

```bash
# Readiness and a request id of your choice
curl -i -H "X-Request-ID: demo-123" http://127.0.0.1:8000/ready

# Find an entity, then fetch it with history
curl -H "Authorization: Bearer $KEY" "http://127.0.0.1:8000/api/v1/entities?q=ledger&type=service"
curl -H "Authorization: Bearer $KEY" "http://127.0.0.1:8000/api/v1/entities/service:ledger-service?history=true"

# Impact analysis
curl -H "Authorization: Bearer $KEY" "http://127.0.0.1:8000/api/v1/dependencies/service:auth-service?direction=upstream"

# Deployments of one application in production
curl -H "Authorization: Bearer $KEY" "http://127.0.0.1:8000/api/v1/deployments?application=application:payments-platform&environment=prod"

# Single-intent question
curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"question": "Which Kubernetes clusters host Customer B'"'"'s applications?"}' \
  http://127.0.0.1:8000/api/v1/query

# Multi-agent workflow
curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"question": "Which applications depend on Payment Service, and where are they deployed?"}' \
  http://127.0.0.1:8000/api/v1/agent/query

# Recent workflow runs, then one run's events
curl -H "Authorization: Bearer $KEY" "http://127.0.0.1:8000/api/v1/agent/runs?limit=5&offset=0"
curl -H "Authorization: Bearer $KEY" "http://127.0.0.1:8000/api/v1/agent/runs/<run id>/events"

# Admin: create a customer-restricted viewer key
curl -X POST -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name": "acme-assistant", "roles": ["viewer"], "allowed_customers": ["acme"]}' \
  http://127.0.0.1:8000/api/v1/admin/api-keys
```

PowerShell, workflow run:

```powershell
$run = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/agent/query -Headers @{Authorization="Bearer $key"} -ContentType "application/json" -Body '{"question":"What happens if Payment Service goes down?"}'
```

```powershell
$run.answer.summary; $run.validation.status
```
