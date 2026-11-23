# Running the platform locally: walkthrough

This walkthrough takes a fresh Windows machine to an AI agent (Claude) answering questions from the
enterprise knowledge graph. It is the path that was actually used for development, including the
pitfalls hit along the way. Linux and macOS work the same way. Commands are PowerShell unless
noted.

There are two ways to run it. Pick one:

| | Option A: Python only | Option B: Podman (recommended) |
|---|---|---|
| Database | SQLite file | PostgreSQL 16 in a container |
| Needs | Python 3.11 | Python 3.11 + Podman |
| Good for | Quick look, tests, development | Realistic setup, MCP over HTTP, same as deployment |

---

## 0. Get the code and a Python environment (both options)

```powershell
git clone https://github.com/Aasrithasreekari/enterprise-ontology-platform.git
```

```powershell
cd enterprise-ontology-platform
```

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\pip install -e ".[dev,postgres]"
```

Check everything works by running the test suite. It should report 129 passed:

```powershell
.venv\Scripts\python -m pytest -q
```

---

## Option A: Python only (SQLite)

1. Load the sample enterprise. This applies migrations, ingests the sample data and prints API keys
   (shown only once; copy them somewhere safe):

   ```powershell
   .venv\Scripts\ontology-platform seed-demo
   ```

2. Ask a question from the command line:

   ```powershell
   .venv\Scripts\ontology-platform ask --tenant northwind "Which Kubernetes clusters host Customer B's applications?"
   ```

3. Optional: run the REST API, then open http://127.0.0.1:8000/docs.

   ```powershell
   .venv\Scripts\ontology-platform serve
   ```

To connect Claude in this mode, use the **stdio** MCP setup in step B6 (alternative).

---

## Option B: Podman (PostgreSQL + REST API + MCP server)

### B1. One-time Podman setup

1. Check that Podman is installed:

   ```powershell
   podman --version
   ```

2. Windows/macOS: create and start the Podman Linux VM (a one-time download of about 1 GB):

   ```powershell
   podman machine init
   ```

   ```powershell
   podman machine start
   ```

   If it says `already running`, that's fine.

3. **Install the Compose provider and pin it.** This is the most common pitfall. If Docker Desktop
   is installed, `podman compose` silently uses Docker's `docker-compose.exe` and fails with errors
   like `open //./pipe/podman-machine-default` or `no configuration file provided`.

   Install the provider:

   ```powershell
   .venv\Scripts\pip install podman-compose
   ```

   Then pin it in Podman's own config, so it applies in every terminal. Create the file
   `%APPDATA%\containers\containers.conf.d\50-compose-provider.conf` with this content (use your
   real path, and keep the **single** quotes):

   ```toml
   [engine]
   compose_providers = ['C:\path\to\enterprise-ontology-platform\.venv\Scripts\podman-compose.exe']
   compose_warning_logs = false
   ```

   Check it. This must print `podman-compose version ...`, not "Docker Compose":

   ```powershell
   podman compose version
   ```

   > Double quotes in that file break **every** `podman` command ("parsing containers.conf"),
   > because TOML treats `\U` as an escape sequence.

### B2. Configure

```powershell
Copy-Item .env.example .env
```

Open `.env` and set `POSTGRES_PASSWORD` to any local password. `.env` is git-ignored.

### B3. Build and start

> Run all `podman compose` commands **from the project folder**. Elsewhere you get
> `compose.yaml: The system cannot find the file specified`.

Build the image (about 1 minute):

```powershell
podman compose build
```

Start the services:

```powershell
podman compose up -d db api mcp
```

Check they are running:

```powershell
podman compose ps
```

Expected: `db` and `api` show `Up ... (healthy)`. `mcp` shows `Up`; it has no healthcheck, so it
never shows "(healthy)".

| Service | Address |
|---|---|
| REST API + docs | http://127.0.0.1:8000/docs |
| MCP endpoint | http://127.0.0.1:8001/mcp |
| PostgreSQL | inside the Podman network only |

### B4. Load data

```powershell
podman compose exec api ontology-platform seed-demo --data-dir /app/sample_data
```

This loads tenants `northwind` (3 customers, 2 products, 6 applications across
GitHub/GitLab/Bitbucket, Confluence, 4 clusters and AWS/Azure/GCP) and `contoso`. It prints admin,
viewer and Acme-only API keys **once**; save them.

> In Git Bash, prefix container paths with `MSYS_NO_PATHCONV=1`, otherwise `/app/sample_data`
> becomes `C:/Program Files/Git/app/...`. PowerShell does not have this problem.

### B5. Try it over REST

```powershell
$key = "<viewer api key>"
```

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/query -Headers @{Authorization="Bearer $key"} -ContentType "application/json" -Body '{"question":"What applications are affected if auth-service goes down?"}'
```

### B6. Connect Claude (MCP)

1. Create a read-only key for Claude:

   ```powershell
   podman compose exec api ontology-platform create-api-key --tenant northwind --name claude-local --roles viewer
   ```

   Add `--customers acme` to restrict it to one customer.

2. Copy the example config and put the key in it. `.mcp.json` is git-ignored because it contains the key.

   ```powershell
   Copy-Item .mcp.json.example .mcp.json
   ```

   Edit `.mcp.json` and replace `REPLACE_WITH_YOUR_API_KEY`.

3. Start a **new** Claude Code session in this folder. MCP servers load only when a session starts.
   Approve the `enterprise-ontology` server when prompted.

4. Check it: ask *"List the tools you have from the enterprise-ontology server"*. It should show 9
   tools. Then ask real questions, for example:
   * "Which repositories belong to Acme Bank's PayHub product?"
   * "Where is claims-portal deployed in UAT and production, and on which clusters?"
   * "What applications and customers are affected if auth-service goes down? Show the evidence."
   * "Which documentation describes the ledger service?"
   * "How is Initech connected to the ocr-worker service?"

   Each tool call appears as `POST /mcp ... 200 OK` in the logs:

   ```powershell
   podman compose logs -f mcp
   ```

   Press Ctrl+C to stop watching; this does not stop the server.

**Alternative (no containers): stdio.** Use this `.mcp.json` instead. Claude starts the server
itself, using the SQLite database from Option A:

```json
{
  "mcpServers": {
    "enterprise-ontology": {
      "command": "C:/path/to/enterprise-ontology-platform/.venv/Scripts/ontology-platform.exe",
      "args": ["mcp", "--transport", "stdio"],
      "env": { "OP_DATABASE_URL": "sqlite:///C:/path/to/enterprise-ontology-platform/ontology.db", "OP_MCP_API_KEY": "op_..." }
    }
  }
}
```

### B7. Daily use

| Task | Command (from the project folder) |
|---|---|
| Start after a reboot | `podman machine start` then `podman compose up -d db api mcp` |
| Status | `podman compose ps` |
| Logs | `podman compose logs -f api` (or `mcp`, `db`) |
| Re-sync data | `podman compose exec api ontology-platform sync --tenant northwind --all` |
| Ask from the CLI | `podman compose exec api ontology-platform ask --tenant northwind "..."` |
| Rebuild after code changes | `podman compose build api` then `podman compose up -d db api mcp` |
| Stop (keep data) | `podman compose down` |
| Stop and **delete** data | `podman compose down -v` |

---

## Connecting real sources

The sample data comes from a local test adapter. To ingest real systems, register a connector with
a secret **reference**. Secrets are never stored in the database.

```powershell
podman compose exec api ontology-platform add-connector --tenant northwind --name github-main --type github --config '{\"org\": \"my-org\"}' --secret-ref env:GITHUB_TOKEN
```

For the container to see `GITHUB_TOKEN`, add it to `.env` and to the `x-app-env` block in
`compose.yaml`. Then sync:

```powershell
podman compose exec api ontology-platform sync --tenant northwind --connector github-main
```

Connector config fields are defined by each `*Config` model in `src/ontology_platform/connectors/`.
The real connectors have only been tested against mock API responses so far (see
IMPLEMENTATION_PLAN.md).

## Optional: PuppyGraph proof of concept

See [poc/puppygraph/FINDINGS.md](../poc/puppygraph/FINDINGS.md). It needs **≥ 8 GB RAM for the
Podman VM** (16 GB host recommended). On Windows, set it in `%UserProfile%\.wslconfig`:

```
[wsl2]
memory=8GB
```

Then run `wsl --shutdown` and `podman machine start`.

```powershell
podman compose -f compose.yaml -f poc/puppygraph/compose.puppygraph.yaml up -d db api puppygraph
```

```powershell
.venv\Scripts\python poc\puppygraph\setup_poc.py
```

```powershell
.venv\Scripts\python poc\puppygraph\run_queries.py
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Executing external compose provider "...Docker...docker-compose.exe"` | Pin the provider (B1 step 3). |
| `open //./pipe/podman-machine-default: The system cannot find the file specified` | Docker's compose is being used (pin the provider), or the machine is stopped (`podman machine start`). |
| `compose.yaml: The system cannot find the file specified` / `no configuration file provided` | Run from the project folder. |
| `Cannot connect to Podman` / `ssh: handshake failed` | VM stopped or overloaded: `podman machine start`; close memory-heavy apps. |
| `set POSTGRES_PASSWORD in .env` | Create `.env` (B2). |
| `api` keeps restarting | `podman compose logs api`. If the DB volume has old credentials, run `podman compose down -v` (deletes data). |
| Claude doesn't list the enterprise-ontology tools | Start a **new** session in the project folder and approve the server. Check that `.mcp.json` has a valid key and the `mcp` container is up. |
| MCP tool error `unauthenticated` | Missing or revoked key in `.mcp.json`. Create a new one (B6). |
| MCP HTTP 421 / "Invalid Host header" | Use `127.0.0.1` or `localhost`, or add your host to `OP_MCP_ALLOWED_HOSTS`. |

More container detail is in [PODMAN.md](PODMAN.md).
