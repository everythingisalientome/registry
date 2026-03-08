# Registry

A Python FastAPI service that manages a pool of desktop agent sessions. It launches, health checks, and pools agent + MCP process pairs, exposing a simple HTTP API for acquiring and releasing sessions. Packaged as a standalone Windows executable via PyInstaller.

---

## Project Structure

```
registry/
├── main.py                     # FastAPI app — session acquire/release/status endpoints
├── session_manager.py          # AgentPool and SessionManager — core pool logic
├── models.py                   # Session dataclass and enums
├── config.yaml                 # Server config (rdsh mode)
├── config.local.yaml           # Local dev config (local mode)
└── dist/
    └── registry/               # PyInstaller output
        ├── registry.exe
        ├── config.yaml
        └── config.local.yaml
```

---

## Endpoints

### `GET /session/acquire?agent_type=desktop`
Acquires an available session from the warm pool. Returns session details including `agent_url`.

**Response:**
```json
{
  "slot": 1,
  "agent_type": "desktop",
  "agent_port": 8001,
  "mcp_port": 5001,
  "type": "warm",
  "status": "busy",
  "agent_url": "http://192.168.0.135:8001",
  "mcp_url": "http://192.168.0.135:5001"
}
```

### `POST /session/release/{agent_port}?agent_type=desktop`
Releases a session back to the pool. Warm sessions are marked available; dynamic sessions are torn down.

### `GET /session/status`
Returns current status of all sessions across all agent types.

---

## Session Modes

### `local`
Launches agent and MCP directly via `subprocess.Popen` on the current desktop. Used for local development and testing.

### `rdsh`
Launches agent and MCP in isolated Windows RDSH user sessions via `SessionLauncher.exe`. Each slot runs under a dedicated user (`agent_user_1`, `agent_user_2`, etc.) so automation is fully isolated.

---

## Config

### `config.yaml` (server — rdsh mode)
```yaml
agent_types:
  desktop:
    session_mode: "rdsh"
    warm_sessions: 3
    max_sessions: 10
    agent_script: "C:/agents/desktop_agent/desktop_agent.exe"
    mcp_binary: "C:/agents/GoogleSearchMcp/GoogleSearchMcp.exe"
    session_launcher: "C:/agents/SessionLauncher/SessionLauncher.exe"
    session_users:
      - "agent_user_1"
      - "agent_user_2"
      - "agent_user_3"
    agent_base_port: 8000
    mcp_base_port: 5000
    host: "192.168.0.135"

health_check_interval_seconds: 30
health_check_timeout_seconds: 5
startup_timeout_seconds: 30
```

### `config.local.yaml` (local dev — local mode)
```yaml
agent_types:
  desktop:
    session_mode: "local"
    warm_sessions: 1
    max_sessions: 3
    agent_script: "D:/VisualStudioCodeWrkSpce/LLMs/demo-desktop-agent/dist/desktop_agent/desktop_agent.exe"
    mcp_binary: "D:/VisualStudioWrkSpce/2022/demo-mcp/GoogleSearchMcp/publish/GoogleSearchMcp.exe"
    session_launcher: ""
    session_users: []
    agent_base_port: 8000
    mcp_base_port: 5000
    host: "localhost"

health_check_interval_seconds: 30
health_check_timeout_seconds: 5
startup_timeout_seconds: 30
```

---

## Build

```bash
cd D:\VisualStudioCodeWrkSpce\LLMs\registry

.venv\Scripts\pyinstaller --onedir --name registry main.py ^
  --hidden-import session_manager ^
  --hidden-import models ^
  --add-binary "C:\Users\PreetPragyan\AppData\Local\Programs\Python\Python312\python312.dll;_internal"
```

Output: `dist\registry\registry.exe`

---

## Run

### Local testing
```bash
cd D:\VisualStudioCodeWrkSpce\LLMs\registry\dist\registry

# Copy config first
copy ..\..\config.local.yaml .

.\registry.exe --config config.local.yaml
```

### On server
```bash
cd C:\agents\registry
.\registry.exe
```
Loads `config.yaml` from the same directory by default.

---

## Deploy to Server

1. Copy `dist\registry\` contents to `C:\agents\registry\`
2. Copy `config.yaml` to `C:\agents\registry\`
3. Stop existing processes:
```powershell
Get-Process registry, desktop_agent, GoogleSearchMcp | Stop-Process -Force
```
4. Restart:
```powershell
.\registry.exe
```

---

## Port Reference

| Component | Local ports | Server ports |
|---|---|---|
| Registry | 9000 | 9000 |
| Desktop Agent slots 1-3 | 8001-8003 | 8001-8003 |
| MCP tool slots 1-3 | 5001-5003 | 5001-5003 |

---

## Health Check Flow

Registry polls each agent's `/health` endpoint every 30 seconds. Dead sessions are automatically restarted.