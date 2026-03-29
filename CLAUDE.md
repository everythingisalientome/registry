# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A FastAPI-based Windows service (`registry.exe`) that manages a pool of AI agent sessions. Each session is a pair of processes — a desktop agent (HTTP on port 8001+) and an MCP server (HTTP on port 5001+). Clients acquire sessions, run automation tasks, then release them.

## Running the Service

```bash
# Local dev — run from the dist output directory
cd dist\registry
copy ..\..\config.local.yaml .
.\registry.exe --config config.local.yaml

# On server (loads config.yaml from same directory by default)
cd C:\agents\registry
.\registry.exe
```

The service runs on `0.0.0.0:9000`.

## Building the Executable

```bash
cd D:\VisualStudioCodeWrkSpce\LLMs\registry

.venv\Scripts\pyinstaller --onedir --name registry main.py ^
  --hidden-import session_manager ^
  --hidden-import models ^
  --add-binary "C:\Users\PreetPragyan\AppData\Local\Programs\Python\Python312\python312.dll;_internal"
```

Output lands in `dist\registry\registry.exe`.

## Installing Dependencies

```bash
pip install -r requirements.txt
```

## Architecture

### Core Files

- **`main.py`** — FastAPI app + uvicorn entry point. Three endpoints: `GET /session/acquire`, `POST /session/release/{agent_port}`, `GET /session/status`.
- **`session_manager.py`** — `SessionManager` holds one `AgentPool` per agent type. `AgentPool` handles the pool lifecycle: warm-up, acquire, release, health monitoring (30s interval).
- **`models.py`** — `Session` dataclass with state enum (`STARTING → AVAILABLE → BUSY → DEAD`) and type (`WARM` / `DYNAMIC`).
- **`config.yaml`** / **`config.local.yaml`** — YAML config drives all runtime behavior (ports, pool sizes, executable paths, mode).

### Two Deployment Modes

| Mode | How Sessions Launch | Isolation |
|------|---------------------|-----------|
| `local` | `subprocess.Popen` directly on current desktop | Shared OS user |
| `rdsh` | Via `SessionLauncher.exe` into isolated Windows RDSH user sessions | Per-user (`agent_user_1`, `agent_user_2`, ...) |

Switch modes via `session_mode` in the config YAML.

### Port Layout

```
Registry:    9000
Agents:      agent_base_port + slot  (default: 8001, 8002, 8003...)
MCP servers: mcp_base_port + slot    (default: 5001, 5002, 5003...)
```

### Session Lifecycle

1. Warm sessions pre-launch at startup up to `warm_sessions` count.
2. `acquire` returns first `AVAILABLE` session (WARM preferred), or spawns a `DYNAMIC` session if under `max_sessions`.
3. On `release`: WARM sessions reset to `AVAILABLE`; DYNAMIC sessions are terminated.
4. A background health-check loop polls each agent's `/health` endpoint every `health_check_interval_seconds` and restarts `DEAD` sessions.

### Config Structure (key fields)

```yaml
agent_types:
  desktop:
    session_mode: "local"           # or "rdsh"
    warm_sessions: 1
    max_sessions: 3
    agent_script: "path/to/desktop_agent.exe"
    mcp_binary: "path/to/GoogleSearchMcp.exe"
    session_launcher: ""            # path to SessionLauncher.exe (rdsh only)
    session_users: []               # ["agent_user_1", ...] (rdsh only)
    agent_base_port: 8000
    mcp_base_port: 5000
    host: "localhost"

health_check_interval_seconds: 30
health_check_timeout_seconds: 5
startup_timeout_seconds: 30
```
