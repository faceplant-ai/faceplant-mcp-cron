import asyncio
import json
import logging
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

logger = logging.getLogger("faceplant-mcp-cron")

BROKER_URL = os.getenv("BROKER_URL", "http://host.docker.internal:5177")
GATEWAY_UPSTREAM = os.getenv("GATEWAY_UPSTREAM", "http://host.docker.internal:5191")
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CRON_DIR = DATA_DIR / "cron"
LOGS_DIR = DATA_DIR / "logs"
VENVS_DIR = DATA_DIR / "venvs"


# ── Cron helpers ──


def _load_jobs() -> dict[str, dict]:
    """Load all job definitions from /data/cron/*.json."""
    jobs = {}
    if CRON_DIR.exists():
        for f in sorted(CRON_DIR.glob("*.json")):
            try:
                job = json.loads(f.read_text())
                jobs[job["name"]] = job
            except Exception:
                pass
    return jobs


def _save_job(job: dict) -> None:
    """Save a job definition to /data/cron/{name}.json."""
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    path = CRON_DIR / f"{job['name']}.json"
    path.write_text(json.dumps(job, indent=2))


def _delete_job_file(name: str) -> None:
    path = CRON_DIR / f"{name}.json"
    path.unlink(missing_ok=True)


def _sync_crontab() -> None:
    """Rebuild the system crontab from all job definitions."""
    jobs = _load_jobs()
    lines = [
        "# Managed by faceplant-mcp-cron — do not edit manually",
        "SHELL=/bin/bash",
    ]
    # Infra env vars (not secrets — needed for broker/gateway communication)
    for var in ("BROKER_URL", "GATEWAY_UPSTREAM"):
        val = os.environ.get(var, "")
        if val:
            lines.append(f'{var}={val}')
    lines.append("")
    for job in jobs.values():
        if not job.get("enabled", True):
            continue
        log_file = LOGS_DIR / f"{job['name']}.log"
        # Inject per-job env vars inline so each job only sees its own keys
        env_prefix = ""
        for k, v in job.get("env", {}).items():
            safe_v = v.replace("'", "'\\''")
            env_prefix += f"{k}='{safe_v}' "
        lines.append(
            f"{job['schedule']} {env_prefix}{job['command']} >> {log_file} 2>&1"
        )
    lines.append("")

    crontab_content = "\n".join(lines)
    result = subprocess.run(
        ["crontab", "-"],
        input=crontab_content,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error(f"crontab install failed: {result.stderr}")

    (DATA_DIR / "crontab").write_text(crontab_content)


def _read_log(name: str, tail: int = 50) -> str:
    log_file = LOGS_DIR / f"{name}.log"
    if not log_file.exists():
        return ""
    lines = log_file.read_text().splitlines()
    return "\n".join(lines[-tail:])


def _setup_venv(name: str, dependencies: list[str]) -> Path:
    """Create or update a per-job venv with the given dependencies."""
    venv_dir = VENVS_DIR / name
    venv_dir.mkdir(parents=True, exist_ok=True)

    # Create venv if it doesn't exist
    if not (venv_dir / "bin" / "python3").exists():
        result = subprocess.run(
            ["uv", "venv", str(venv_dir)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"uv venv failed: {result.stderr}")

    # Install dependencies
    result = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_dir / "bin" / "python3"), *dependencies],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"uv pip install failed: {result.stderr}")

    return venv_dir


def _fetch_keys(key_names: list[str]) -> dict[str, str]:
    """Fetch key values from connections service via broker. Returns {name: value}.
    Raises RuntimeError if any key is missing or access denied."""
    env = {}
    errors = []
    for key_name in key_names:
        try:
            resp = httpx.post(
                f"{BROKER_URL}/request/connections.get-key",
                json={"data": {"key_name": key_name, "caller": "faceplant-mcp-cron"}},
                timeout=5,
            )
            if resp.status_code == 403:
                errors.append(f"{key_name}: access denied — grant mcp-cron access in Connections settings")
            elif resp.status_code == 404:
                errors.append(f"{key_name}: not found in Connections")
            elif resp.status_code != 200:
                errors.append(f"{key_name}: broker error ({resp.status_code})")
            else:
                data = resp.json()
                env[key_name] = data["value"]
        except httpx.ConnectError:
            errors.append(f"{key_name}: connections service unreachable (broker down?)")
        except Exception as e:
            errors.append(f"{key_name}: {e}")
    if errors:
        raise RuntimeError("Failed to fetch keys:\n" + "\n".join(f"  - {e}" for e in errors))
    return env


def _create_job(name: str, schedule: str, command: str, enabled: bool = True,
                script: str | None = None, dependencies: list[str] | None = None,
                keys: list[str] | None = None) -> dict:
    """Create or update a cron job. Optionally writes a script and sets up a venv.
    If keys are specified, fetches them from connections at submission time."""
    venv_python = "/data/venv/bin/python3"  # fallback to shared venv

    # Fetch keys from connections before anything else — fail fast
    env = {}
    if keys:
        env = _fetch_keys(keys)

    if dependencies:
        venv_dir = _setup_venv(name, dependencies)
        venv_python = str(venv_dir / "bin" / "python3")

    if script is not None:
        script_path = CRON_DIR / f"{name}.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script)
        if not command:
            command = f"cd /data && {venv_python} {script_path}"

    job = {
        "name": name,
        "schedule": schedule,
        "command": command,
        "enabled": enabled,
        "dependencies": dependencies or [],
        "keys": keys or [],
        "env": env,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _save_job(job)
    _sync_crontab()
    return job


def _run_job(name: str) -> dict:
    """Trigger a job immediately. Returns exit code and output."""
    jobs = _load_jobs()
    if name not in jobs:
        return {"error": f"Job '{name}' not found"}

    job = jobs[name]
    log_file = LOGS_DIR / f"{name}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Build env: inherit current env + job-specific keys
    run_env = os.environ.copy()
    run_env.update(job.get("env", {}))

    with open(log_file, "a") as f:
        f.write(f"\n--- manual run {datetime.now(timezone.utc).isoformat()} ---\n")
        result = subprocess.run(
            job["command"],
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
            env=run_env,
        )
        f.write(result.stdout)
        if result.stderr:
            f.write(result.stderr)

    return {
        "name": name,
        "exit_code": result.returncode,
        "stdout": result.stdout[-500:] if result.stdout else "",
        "stderr": result.stderr[-500:] if result.stderr else "",
    }


# ── MCP Server ──


mcp = FastMCP(
    "faceplant-mcp-cron",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


@mcp.tool()
def list_jobs() -> str:
    """List all scheduled cron jobs. Returns JSON with job names, schedules, commands, and status."""
    jobs = _load_jobs()
    if not jobs:
        return "No cron jobs configured."
    # Strip env values (secrets) — only show key names
    safe = []
    for job in jobs.values():
        j = {k: v for k, v in job.items() if k != "env"}
        safe.append(j)
    return json.dumps(safe, indent=2)


@mcp.tool()
def create_job(name: str, schedule: str, command: str = "", enabled: bool = True,
               script: str = "", dependencies: list[str] = [],
               keys: list[str] = []) -> str:
    """Create or update a cron job, optionally deploying a Python script and its dependencies.

    When script is provided, it is written to /data/cron/{name}.py. When dependencies
    are provided, a per-job venv is created at /data/venvs/{name}/ using uv. If command
    is omitted, a default is generated that runs the script with the job's venv Python.

    When keys are provided, they are fetched from the Connections service at submission
    time and stored as environment variables for the job. If any key is missing or
    mcp-cron doesn't have access, the job is rejected with an error.

    Args:
        name: Unique job name (e.g. "daily-standup")
        schedule: Cron expression (e.g. "0 9 * * 1-5" for 9am weekdays)
        command: Shell command to execute (auto-generated if script is provided and command is empty)
        enabled: Whether the job is active (default: true)
        script: Full Python source code to deploy alongside the job (optional)
        dependencies: List of pip packages for the job's venv (e.g. ["anthropic", "slack-sdk", "requests"])
        keys: List of API key names from Connections (e.g. ["NOTION_API_KEY", "ANTHROPIC_API_KEY"])
    """
    try:
        job = _create_job(name, schedule, command, enabled,
                          script=script or None, dependencies=dependencies or None,
                          keys=keys or None)
    except RuntimeError as e:
        return str(e)
    safe = {k: v for k, v in job.items() if k != "env"}
    return json.dumps(safe, indent=2)


@mcp.tool()
def delete_job(name: str) -> str:
    """Delete a cron job by name. Removes the job definition, script, and venv.

    Args:
        name: The job name to delete
    """
    jobs = _load_jobs()
    if name not in jobs:
        return f"Job '{name}' not found."
    _delete_job_file(name)
    # Clean up script
    script_path = CRON_DIR / f"{name}.py"
    script_path.unlink(missing_ok=True)
    # Clean up per-job venv
    venv_dir = VENVS_DIR / name
    if venv_dir.exists():
        import shutil
        shutil.rmtree(venv_dir, ignore_errors=True)
    _sync_crontab()
    return f"Deleted job '{name}'."


@mcp.tool()
def run_job(name: str) -> str:
    """Trigger a cron job immediately and return its output.

    Args:
        name: The job name to run
    """
    result = _run_job(name)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_job_script(name: str) -> str:
    """Get the source code of a cron job's script.

    Parses the job's command to find the Python script path and returns its contents.
    Use this to understand what a job does or before making changes with update_job_script.

    Args:
        name: The job name
    """
    jobs = _load_jobs()
    if name not in jobs:
        return f"Job '{name}' not found."

    command = jobs[name]["command"]

    # Extract .py script path from command string
    script_path = None
    for part in command.split():
        if part.endswith(".py"):
            script_path = Path(part)
            break

    if not script_path:
        return f"Could not find a .py script in command: {command}"

    if not script_path.exists():
        return f"Script not found at {script_path}"

    return script_path.read_text()


@mcp.tool()
def update_job_script(name: str, script: str) -> str:
    """Update the source code of a cron job's script.

    Writes the provided script content to the job's script file, replacing the existing code.
    The job's command and schedule remain unchanged.

    Args:
        name: The job name
        script: The full Python script source code to write
    """
    jobs = _load_jobs()
    if name not in jobs:
        return f"Job '{name}' not found."

    command = jobs[name]["command"]

    # Extract .py script path from command string
    script_path = None
    for part in command.split():
        if part.endswith(".py"):
            script_path = Path(part)
            break

    if not script_path:
        return f"Could not find a .py script in command: {command}"

    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script)
    return f"Updated {script_path} ({len(script)} bytes)"


@mcp.tool()
def get_job_logs(name: str, tail: int = 50) -> str:
    """Get recent log output for a cron job.

    Args:
        name: The job name
        tail: Number of lines to return (default: 50)
    """
    jobs = _load_jobs()
    if name not in jobs:
        return f"Job '{name}' not found."
    log = _read_log(name, tail=tail)
    return log if log else "No log output yet."


# ── MCP ASGI app ──

# Build the MCP Starlette app (needed for lifespan/session manager init).
# We do NOT mount it via app.mount() because Starlette's Mount always
# redirects /mcp → /mcp/ (307), which breaks behind a reverse proxy.
# Instead we extract the ASGI handler and invoke it from a FastAPI route.
_mcp_starlette = mcp.streamable_http_app()
_mcp_asgi_app = _mcp_starlette.routes[0].app  # StreamableHTTPASGIApp
mcp_app = _mcp_starlette  # keep reference for lifespan


# ── Broker registration ──


async def _register_loop():
    """Register with gateway and register responders with broker."""
    await asyncio.sleep(1)

    gateway_payload = {"data": {
        "path": "/api/mcp-cron/",
        "service": "faceplant-mcp-cron",
        "upstream": GATEWAY_UPSTREAM,
        "label": "MCP Cron",
        "description": "Scheduled job runner with persistent cron definitions.",
        "icon": "clock",
        "category": "mcp",
        "cta": "View",
    }}

    mcp_registration = {
        "service": "faceplant-mcp-cron",
        "url": f"{GATEWAY_UPSTREAM}/mcp",
    }

    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(f"{BROKER_URL}/publish/gateway.register", json=gateway_payload)
                await c.put(f"{BROKER_URL}/mcp/mcp-cron", json=mcp_registration)
        except Exception:
            pass
        await asyncio.sleep(30)


# ── Lifespan (combines FastAPI + MCP session manager) ──


@asynccontextmanager
async def lifespan(app: FastAPI):
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _sync_crontab()
    asyncio.create_task(_register_loop())
    # Enter the MCP session manager context
    async with mcp_app.router.lifespan_context(app):
        yield


app = FastAPI(title="faceplant-mcp-cron", lifespan=lifespan)

origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

base_url = os.getenv("BASE_URL", "/api/mcp-cron")

# ── MCP route ──
# Use a Starlette Route with a class-based ASGI app (not app.mount) to
# avoid the Mount 307 redirect (/mcp → /mcp/) that breaks reverse proxies.
# Route treats a callable class as a raw ASGI app, preserving SSE streaming.

class _McpAsgiWrapper:
    """Thin wrapper that fixes the path before forwarding to the MCP ASGI app."""
    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope["path"] = "/mcp"
        await _mcp_asgi_app(scope, receive, send)

from starlette.routing import Route as _Route
app.routes.insert(0, _Route("/mcp", endpoint=_McpAsgiWrapper(), methods=["GET", "POST", "DELETE"]))


# ── Models ──


class HealthResponse(BaseModel):
    status: str


class ComponentNode(BaseModel):
    type: str
    props: dict[str, Any] = {}
    children: list["ComponentNode | str"] = []


class CardConfig(BaseModel):
    width: str = "full"
    height: str = "full"


class ManifestResponse(BaseModel):
    card: CardConfig = CardConfig()
    root: ComponentNode


# ── REST Endpoints (dashboard UI) ──


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/manifest", response_model=ManifestResponse)
def manifest() -> ManifestResponse:
    return ManifestResponse(
        root=ComponentNode(
            type="Table",
            props={
                "endpoint": f"{base_url}/data",
                "refreshInterval": 10000,
            },
        ),
    )


@app.get("/data")
def data() -> dict:
    """Return jobs in generic Table widget format."""
    jobs = _load_jobs()
    rows = []
    for job in jobs.values():
        last_line = _read_log(job["name"], tail=1)
        rows.append({
            "name": job["name"],
            "schedule": job["schedule"],
            "command": job["command"][:60] + ("..." if len(job["command"]) > 60 else ""),
            "enabled": "active" if job.get("enabled", True) else "disabled",
            "last_output": last_line[:80] if last_line else "—",
        })

    return {
        "title": "Cron Jobs",
        "columns": [
            {"key": "name", "header": "Name"},
            {"key": "schedule", "header": "Schedule", "type": "code"},
            {"key": "command", "header": "Command", "type": "code"},
            {"key": "enabled", "header": "Status", "type": "badge"},
            {"key": "last_output", "header": "Last Output"},
        ],
        "rows": rows,
    }


