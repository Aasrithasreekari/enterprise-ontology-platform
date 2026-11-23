#!/usr/bin/env bash
# One-shot PuppyGraph gate run on a Linux host with Podman (see HOST_RUNBOOK.md).
#
#   bash poc/puppygraph/run_gates.sh            # full: ~100k entities, concurrency 1 and 10
#   SCALE_CUSTOMERS=50 bash poc/puppygraph/run_gates.sh   # quick smoke run
#
# Produces poc/puppygraph/gate_results/<timestamp>/ with environment info, logs and JSON results.
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT=$(pwd)
OUT="poc/puppygraph/gate_results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"
COMPOSE=(podman compose -f compose.yaml -f poc/puppygraph/compose.puppygraph.yaml)
SCALE_CUSTOMERS=${SCALE_CUSTOMERS:-500}
ITERATIONS=${ITERATIONS:-30}
CONCURRENCY=${CONCURRENCY:-"1 10"}

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/run.log"; }

[ -f .env ] || { echo "missing .env (copy .env.example and set POSTGRES_PASSWORD and PUPPYGRAPH_PASSWORD)"; exit 1; }
set -a; . ./.env; set +a
: "${POSTGRES_PASSWORD:?}"; : "${PUPPYGRAPH_PASSWORD:?}"

log "environment"
{
  uname -a; echo; nproc; free -g; echo; grep -o -m1 avx2 /proc/cpuinfo || echo "NO AVX2 (PuppyGraph requires it on amd64)"
  echo; podman --version; podman compose version 2>&1 | tail -1; podman info --format '{{.Host.Security.Rootless}} rootless, cgroups {{.Host.CgroupsVersion}}'
} > "$OUT/environment.txt" 2>&1
cat "$OUT/environment.txt" | tee -a "$OUT/run.log"
if podman compose version 2>&1 | grep -qi "docker compose"; then
  echo "podman compose is using Docker Compose as provider; set PODMAN_COMPOSE_PROVIDER to podman-compose" | tee -a "$OUT/run.log"; exit 1
fi

if [ ! -x .venv/bin/python ]; then
  log "creating virtualenv"
  python3.11 -m venv .venv
  .venv/bin/pip install -q -e ".[dev,postgres]"
fi
PY=.venv/bin/python
DB_URL="postgresql+psycopg://${POSTGRES_USER:-ontology}:${POSTGRES_PASSWORD}@127.0.0.1:${PG_HOST_PORT:-5432}/${POSTGRES_DB:-ontology}"

log "build + start stack (db, api, puppygraph)"
"${COMPOSE[@]}" build api >> "$OUT/run.log" 2>&1
"${COMPOSE[@]}" up -d db api puppygraph >> "$OUT/run.log" 2>&1
for i in $(seq 1 60); do curl -sf http://127.0.0.1:8000/ready >/dev/null && break; sleep 3; done
API=$(podman ps --format '{{.Names}}' | grep -E 'ontology-platform[-_]api[-_]1' | head -1)
DB=$(podman ps --format '{{.Names}}' | grep -E 'ontology-platform[-_]db[-_]1' | head -1)

log "seed sample tenants (idempotent)"
podman exec "$API" ontology-platform seed-demo --data-dir /app/sample_data > "$OUT/seed.json" 2>>"$OUT/run.log" || true

log "generate scale tenant (customers=$SCALE_CUSTOMERS)"
$PY poc/puppygraph/scale_data.py --db "$DB_URL" --customers "$SCALE_CUSTOMERS" | tee -a "$OUT/run.log"

log "graph views + PuppyGraph schema"
PUPPYGRAPH_PASSWORD="$PUPPYGRAPH_PASSWORD" $PY poc/puppygraph/setup_poc.py --db-container "$DB" | tee -a "$OUT/run.log"

log "use-case queries (sample tenant)"
$PY poc/puppygraph/run_queries.py --password "$PUPPYGRAPH_PASSWORD" --json "$OUT/use_cases.json" | tee -a "$OUT/run.log" || true

log "benchmark (iterations=$ITERATIONS, concurrency=$CONCURRENCY)"
# shellcheck disable=SC2086
$PY poc/puppygraph/benchmark.py --db "$DB_URL" --puppygraph-password "$PUPPYGRAPH_PASSWORD" \
  --iterations "$ITERATIONS" --concurrency $CONCURRENCY --out "$OUT/benchmark.json" | tee -a "$OUT/run.log" || true

log "collect container stats and PuppyGraph memory errors"
podman stats --no-stream > "$OUT/podman_stats.txt" 2>&1 || true
PG=$(podman ps --format '{{.Names}}' | grep -E 'puppygraph' | head -1)
{ echo "OutOfMemoryError count: $(podman logs "$PG" 2>&1 | grep -c OutOfMemoryError || true)";
  echo "DAE memory-limit errors: $(podman logs "$PG" 2>&1 | grep -c 'exceed limit' || true)"; } | tee "$OUT/puppygraph_memory.txt"

log "done -> $OUT (share this directory)"
