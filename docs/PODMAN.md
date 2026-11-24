# Running with Podman

The platform's container setup uses **Podman** and **Podman Compose** only: a `Containerfile` and a
Compose-spec `compose.yaml`. Docker is not required.

| Service | Image | Port (host, loopback only) | Purpose |
|---|---|---|---|
| `db` | `docker.io/library/postgres:16-alpine` | not published | PostgreSQL, named volume `pgdata` |
| `api` | `localhost/ontology-platform:0.1.0` | `127.0.0.1:8000` | Runs migrations (waits for `db`), then REST API |
| `mcp` | same image | `127.0.0.1:8001` | MCP over streamable HTTP at `/mcp` |

All three are attached to the `ontology` network. Containers reach each other by service name:
`api`/`mcp` → `db:5432`.

## 1. Prerequisites

```powershell
podman --version
```

**Windows and macOS** need a Podman machine (a Linux VM). Create it once:

```powershell
podman machine init
```

```powershell
podman machine start
```

```powershell
podman info
```

**Compose provider.** `podman compose` hands the work to a provider. Install `podman-compose`:

```powershell
pip install podman-compose
```

```powershell
podman compose version
```

**Pin the provider.** If Docker Desktop's `docker-compose` is installed, `podman compose` may
silently use it; check whether `podman compose version` prints "Docker Compose". To keep Docker out of the toolchain, and also when `podman-compose` lives
only inside a virtualenv, point Podman at `podman-compose` explicitly:

```powershell
$env:PODMAN_COMPOSE_PROVIDER = (Get-Command podman-compose).Source
```

**Most reliable (Windows): pin it in Podman's own config.** This works in every terminal,
including ones that were already open, because it doesn't depend on environment variables. Create
`%APPDATA%\containers\containers.conf.d\50-compose-provider.conf`:

```toml
[engine]
compose_providers = ['C:\path\to\podman-compose.exe']   # single quotes: TOML literal string
compose_warning_logs = false
```

Use **single** quotes. In a double-quoted TOML string `\U...` is read as an escape, which breaks
*every* `podman` command with a "parsing containers.conf" error. On Linux the equivalent file is
`~/.config/containers/containers.conf`.

The `$env:` setting above lasts only for the current window. To make it permanent for your user (new windows
pick it up), run this from the project folder:

```powershell
[Environment]::SetEnvironmentVariable("PODMAN_COMPOSE_PROVIDER", "$PWD\.venv\Scripts\podman-compose.exe", "User")
```

All `podman compose` commands must run **from the project folder**, because `compose.yaml` and the
`-f` paths are relative.

## 2. Configure

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set at least `POSTGRES_PASSWORD`. Compose reads `.env` from the project directory.
`.env` is git-ignored. Connector secrets go in `.env` as plain variables (for example
`GITHUB_TOKEN=...`) and connectors reference them as `secret_ref: env:GITHUB_TOKEN`. To make them
visible inside the containers, add them to the `x-app-env` block in `compose.yaml`.

## 3. Build and start

```powershell
podman compose build
```

```powershell
podman compose up -d
```

```powershell
podman ps
```

```powershell
podman compose ps
```

To build the image on its own without Compose:

```powershell
podman build -t localhost/ontology-platform:0.1.0 -f Containerfile .
```

```powershell
podman images localhost/ontology-platform
```

## 4. Load the sample dataset and get API keys

```powershell
podman compose exec api ontology-platform seed-demo --data-dir /app/sample_data
```

This prints admin and viewer API keys for tenants `northwind` and `contoso`, plus an Acme-only
viewer key. The keys are shown only once.

```powershell
curl.exe -H "Authorization: Bearer <viewer key>" "http://127.0.0.1:8000/api/v1/entities?q=ledger"
```

OpenAPI docs: http://127.0.0.1:8000/docs. Readiness: http://127.0.0.1:8000/ready. Metrics: http://127.0.0.1:8000/metrics.

MCP endpoint for agents: `http://127.0.0.1:8001/mcp`, with header `Authorization: Bearer <api key>`.

## 5. Day-to-day operations

| Task | Command |
|---|---|
| Logs (all / one service) | `podman compose logs -f` / `podman compose logs -f api` |
| Logs of a container | `podman logs -f ontology-platform_api_1` |
| Shell into a container | `podman exec -it ontology-platform_api_1 /bin/sh` |
| Run a CLI command | `podman compose exec api ontology-platform sync --tenant northwind --all` |
| Ask a question | `podman compose exec api ontology-platform ask --tenant northwind "Which clusters host Customer B's applications?"` |
| psql | `podman compose exec db psql -U ontology -d ontology` |
| Rebuild after code changes | `podman compose build api` then `podman compose up -d` |
| Stop (keep data) | `podman compose down` |
| Stop and delete data | `podman compose down -v` |
| One-off container | `podman run --rm localhost/ontology-platform:0.1.0 --help` |

Container names come from the Compose project name (`ontology-platform`). Check `podman ps` for the
exact names, since the separator differs between Compose providers.

## 6. Rootless notes

* The app runs as UID 10001 inside the container, and Postgres runs as its own unprivileged user.
  Both work under rootless Podman's user namespace.
* Only named volumes are used, with no bind mounts, so no SELinux `:Z` relabelling or host
  permission fixes are needed. Sample data is baked into the image.
* Published ports are above 1024 and bound to `127.0.0.1`. To expose them on the LAN, change the
  `ports` entries; rootless Podman cannot bind ports below 1024 by default.
* Base images use fully-qualified names (`docker.io/library/...`), which avoids Podman's
  short-name resolution prompt.

## 7. PuppyGraph PoC overlay

See [poc/puppygraph/FINDINGS.md](../poc/puppygraph/FINDINGS.md). It needs `PUPPYGRAPH_PASSWORD` in
`.env` and, per the vendor, at least 8 GB RAM for the Podman machine.

```powershell
podman compose -f compose.yaml -f poc/puppygraph/compose.puppygraph.yaml up -d db api puppygraph
```

```powershell
.venv\Scripts\python poc\puppygraph\setup_poc.py
```

On Windows, give the machine more memory through WSL: set `memory=` under `[wsl2]` in
`%UserProfile%\.wslconfig`, then run `wsl --shutdown` and `podman machine start`.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Cannot connect to Podman` / `unable to connect to Podman socket` | The Podman machine is not running: `podman machine start`. If none exists: `podman machine init`. |
| `podman compose` says no provider found | Install `podman-compose`, or set `PODMAN_COMPOSE_PROVIDER` (see §1). |
| `set POSTGRES_PASSWORD in .env` | `.env` is missing or empty. Copy it from `.env.example`. |
| `api` restarts with `database not reachable after 90s` | `db` failed to start. Check `podman compose logs db`. A volume left over from older credentials needs `podman compose down -v` (this deletes data). |
| MCP returns HTTP 421 / "Invalid Host header" | The Host header is not in `OP_MCP_ALLOWED_HOSTS`. Add your hostname, e.g. `myhost:*`. |
| MCP tool result `unauthenticated` | Send `Authorization: Bearer <api key or JWT>`. The HTTP transport has no default identity. |
| Port already in use | Change `OP_API_PORT` / `OP_MCP_PORT` in `.env`. |
| Healthcheck shows nothing | With the OCI image format Podman ignores the Containerfile `HEALTHCHECK`. Health checks are defined in `compose.yaml` instead; inspect with `podman healthcheck run <container>`. |
| `open C:\Users\<you>\compose.yaml: The system cannot find the file specified` | The command was run outside the project folder. `cd` into it first. |
| `Executing external compose provider "...Docker\...\docker-compose.exe"`, or `podman compose version` prints "Docker Compose" | Docker Desktop's provider was picked. Set `PODMAN_COMPOSE_PROVIDER` to `podman-compose` (see §1). |
| Paths like `/app/sample_data` turn into `C:/Program Files/Git/...` | Git Bash path conversion. Use PowerShell, or prefix the command with `MSYS_NO_PATHCONV=1`. |
| PuppyGraph: `Cypher engine unavailable` | Still starting (~60 s), or the JVM ran out of memory (`podman logs` shows `OutOfMemoryError`). Give the machine more RAM and avoid unbounded/untyped `shortestPath`. |
| Slow first build | Dependencies are downloaded once and then cached in image layers. Later code-only changes rebuild only the final layers. |
