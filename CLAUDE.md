# faceplant-mcp-cron

Scheduled job runner for the Faceplant platform. Exposes an **MCP server** (Streamable HTTP) for tool-based access and minimal REST endpoints for the dashboard UI.

## Commands

```bash
just build      # stop old container, build Docker image
just dev        # stop old container, run at http://localhost:5191
just shutdown   # stop the container
```

**Workflow**: run `just build` whenever you change code, then `just dev` to launch. `uv` manages all Python dependencies inside Docker.

## Architecture

FastAPI server + MCP server + system cron daemon inside a Docker container. Job definitions stored as JSON in `/data/cron/`, logs in `/data/logs/`. The `/data` volume persists across redeployments (Docker volume locally, EFS in prod).

The MCP server is mounted at `/mcp` using Streamable HTTP transport (stateless). Claude Code connects to it as a remote MCP server for tool discovery and execution.

## MCP Server

**Endpoint**: `/mcp` (Streamable HTTP)

**Connecting from Claude Code**:
```bash
claude mcp add --transport http mcp-cron http://mcp-cron.faceplant.svc.cluster.local:8000/mcp
```

**Tools**:

| Tool | Description |
|---|---|
| `list_jobs` | List all scheduled cron jobs |
| `create_job(name, schedule, command, enabled)` | Create or update a cron job |
| `delete_job(name)` | Delete a job by name |
| `run_job(name)` | Trigger a job immediately, return output |
| `get_job_logs(name, tail)` | Get recent log output for a job |

## REST API (dashboard only)

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check |
| `/manifest` | GET | UI component tree for the agent card |
| `/data` | GET | Jobs in Table widget format |

All CRUD operations go through the MCP server. The broker registers the MCP endpoint at `mcp/mcp-cron` for proxy access.

## Environment

- `BROKER_URL` - faceplant broker (default: http://host.docker.internal:5177)
- `GATEWAY_UPSTREAM` - this service URL (default: http://host.docker.internal:5191)
- `BASE_URL` - API path prefix (default: /api/mcp-cron)
- `ALLOWED_ORIGINS` - CORS origins (default: http://localhost:5173)
