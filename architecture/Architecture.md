# Multi-Session Desktop Automation System

A distributed system for running parallel, isolated AI-driven desktop automation tasks on Windows Server 2022 RDSH. Each task runs inside a dedicated Windows user session with its own AI agent and MCP tool server.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT / CALLER                          │
│              HTTP POST /session/acquire + /run                  │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    REGISTRY  :9000                               │
│                   registry.exe                                   │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                  SessionManager                          │    │
│  │   AgentPool (desktop)                                    │    │
│  │   ├── Slot 1  agent:8001  mcp:5001  user:agent_user_1   │    │
│  │   ├── Slot 2  agent:8002  mcp:5002  user:agent_user_2   │    │
│  │   └── Slot 3  agent:8003  mcp:5003  user:agent_user_3   │    │
│  └─────────────────────────────────────────────────────────┘    │
└──────────┬──────────────────────────────────────────────────────┘
           │  launches via
           │  SessionLauncher.exe --user agent_user_N
           │
           ├──────────────────────────────────────────────────────┐
           │                                                      │
           ▼                                                      ▼
┌──────────────────────┐                            ┌────────────────────────┐
│  Desktop Agent :800N │                            │  MCP Tool Server :500N │
│  desktop_agent.exe   │◄──── MCP HTTP :500N ──────►│  GoogleSearchMcp.exe   │
│                      │                            │                        │
│  LangGraph ReAct     │                            │  Tools:                │
│  + OpenAI gpt-4o     │                            │  - open_notepad_and_type│
│                      │                            │  - google_search       │
└──────────────────────┘                            └────────────────────────┘
           │
           │  WTSQueryUserToken / CreateProcessAsUser
           │  (runs inside agent_user_N RDSH session)
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│              Windows RDSH Session (agent_user_N)                  │
│                                                                   │
│   Notepad.exe  ◄── FlaUI + SendKeys automation                   │
│   Screenshot   ──► base64 PNG ──► agent ──► caller               │
└──────────────────────────────────────────────────────────────────┘
```

---

## Component Interaction

### 1. Startup Flow
```
registry.exe starts
  └─► SessionManager reads config.yaml
        └─► AgentPool.startup() — launches N warm sessions
              └─► For each slot:
                    ├─► SessionLauncher.exe --user agent_user_N --exe GoogleSearchMcp.exe --args "--urls http://localhost:500N"
                    └─► SessionLauncher.exe --user agent_user_N --exe desktop_agent.exe --args "--port 800N --mcp-url http://localhost:500N"
                          └─► Polls GET /health until agent responds 200
```

### 2. Task Execution Flow
```
Client
  └─► GET /session/acquire?agent_type=desktop  →  Registry
        └─► Returns { agent_url: "http://192.168.0.135:8001", slot: 1, ... }

Client
  └─► POST http://192.168.0.135:8001/run { "task": "Open notepad and type Hello" }
        └─► Desktop Agent
              ├─► Connects to MCP at http://localhost:5001
              ├─► Loads tools: [open_notepad_and_type, google_search]
              ├─► LangGraph invokes tool: open_notepad_and_type("Hello")
              │     └─► MCP Tool (in agent_user_1 session)
              │           ├─► Opens Notepad in user session
              │           ├─► Types text via SendKeys
              │           ├─► Takes screenshot
              │           └─► Returns base64 PNG
              └─► Returns { result: "done", screenshot_base64: "..." }

Client
  └─► POST /session/release/8001?agent_type=desktop  →  Registry
        └─► Marks slot 1 as available
```

### 3. Health Check Loop
```
Every 30 seconds:
  Registry polls GET /health on each agent
    └─► If unhealthy → kill + relaunch session automatically
```

---

## Deployment Folder Structure (Windows Server)

```
C:\agents\
│
├── registry\
│   ├── registry.exe            ← PyInstaller build
│   ├── config.yaml             ← Server config (rdsh mode)
│   └── _internal\              ← PyInstaller runtime deps
│
├── desktop_agent\
│   ├── desktop_agent.exe       ← PyInstaller build
│   ├── .env                    ← OpenAI keys + MCP_URL fallback
│   ├── agent.log               ← Runtime log (per slot, overwrites)
│   └── _internal\              ← PyInstaller runtime deps
│
├── GoogleSearchMcp\
│   ├── GoogleSearchMcp.exe     ← .NET self-contained publish
│   ├── notepad_tool.log        ← NotepadTool runtime log
│   └── *.dll                   ← .NET runtime deps
│
└── SessionLauncher\
    ├── SessionLauncher.exe     ← .NET self-contained publish
    ├── launcher.log            ← Runtime log
    └── *.dll                   ← .NET runtime deps
```

---

## Development Folder Structure

```
D:\VisualStudioCodeWrkSpce\LLMs\
│
├── registry\
│   ├── main.py
│   ├── session_manager.py
│   ├── models.py
│   ├── config.yaml
│   ├── config.local.yaml
│   └── dist\registry\          ← PyInstaller output
│
└── demo-desktop-agent\
    ├── main.py
    ├── desktop_agent_runner.py
    ├── .env
    └── dist\desktop_agent\     ← PyInstaller output

D:\VisualStudioWrkSpce\2022\
│
├── demo-mcp\GoogleSearchMcp\
│   ├── Program.cs
│   ├── Tools\
│   │   ├── NotepadTool.cs
│   │   └── GoogleSearchTool.cs
│   └── publish\                ← dotnet publish output
│
└── SessionLauncher\SessionLauncher\
    ├── Program.cs
    └── publish\                ← dotnet publish output
```

---

## Build Commands Summary

### GoogleSearchMcp
```bash
cd D:\VisualStudioWrkSpce\2022\demo-mcp\GoogleSearchMcp
dotnet publish -c Release -r win-x64 --self-contained true -o ./publish
```

### SessionLauncher
```bash
cd D:\VisualStudioWrkSpce\2022\SessionLauncher\SessionLauncher
dotnet publish -c Release -r win-x64 --self-contained true -o ./publish
```

### Desktop Agent
```bash
cd D:\VisualStudioCodeWrkSpce\LLMs\demo-desktop-agent
.venv\Scripts\pyinstaller --onedir --name desktop_agent main.py ^
  --hidden-import desktop_agent_runner ^
  --hidden-import langchain_mcp_adapters ^
  --hidden-import langchain_openai ^
  --hidden-import langgraph ^
  --hidden-import dotenv ^
  --add-binary "C:\Users\PreetPragyan\AppData\Local\Programs\Python\Python312\python312.dll;_internal"
```

### Registry
```bash
cd D:\VisualStudioCodeWrkSpce\LLMs\registry
.venv\Scripts\pyinstaller --onedir --name registry main.py ^
  --hidden-import session_manager ^
  --hidden-import models ^
  --add-binary "C:\Users\PreetPragyan\AppData\Local\Programs\Python\Python312\python312.dll;_internal"
```

---

## Server Deployment Checklist

- [ ] Windows Server 2022 with RDSH role enabled
- [ ] Users `agent_user_1`, `agent_user_2`, `agent_user_3` created
- [ ] All 3 users added to Remote Desktop Users group
- [ ] All 3 users have active RDP sessions open
- [ ] Firewall rules open: ports 9000, 8001-8003
- [ ] `C:\agents\desktop_agent\.env` contains OpenAI keys
- [ ] Registry runs as Administrator
- [ ] `query session` shows all 3 agent users as Active

## Server Start / Restart Procedure

```powershell
# Kill all existing processes
Get-Process registry, desktop_agent, GoogleSearchMcp, SessionLauncher | Stop-Process -Force

# Start registry (launches everything automatically)
cd C:\agents\registry
.\registry.exe
```

---

## Port Reference

| Component | Port |
|---|---|
| Registry API | 9000 |
| Desktop Agent Slot 1 | 8001 |
| Desktop Agent Slot 2 | 8002 |
| Desktop Agent Slot 3 | 8003 |
| MCP Tool Slot 1 | 5001 |
| MCP Tool Slot 2 | 5002 |
| MCP Tool Slot 3 | 5003 |

---

## Session Modes

| Mode | Use Case | Launch Method |
|---|---|---|
| `local` | Dev/testing on dev machine | Direct `subprocess.Popen` |
| `rdsh` | Production on Windows Server | `SessionLauncher.exe` via `CreateProcessAsUser` |

Switch by changing `session_mode` in `config.yaml`.