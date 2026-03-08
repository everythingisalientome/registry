import asyncio
import logging
import os
import subprocess
import sys
import httpx
import yaml
from models import Session, SessionStatus, SessionType

logger = logging.getLogger(__name__)


class AgentPool:
    """Manages sessions for a single agent type."""

    def __init__(self, agent_type: str, config: dict, global_config: dict):
        self.agent_type = agent_type
        self.warm_count = config["warm_sessions"]
        self.max_sessions = config["max_sessions"]
        self.agent_base_port = config["agent_base_port"]
        self.mcp_base_port = config["mcp_base_port"]
        self.agent_script = config["agent_script"]
        self.mcp_binary = config["mcp_binary"]
        self.host = config.get("host", "localhost")
        #self.python_executable = config.get("python_executable", sys.executable)
        self.startup_timeout = global_config["startup_timeout_seconds"]
        self.health_interval = global_config["health_check_interval_seconds"]
        self.health_timeout = global_config["health_check_timeout_seconds"]
        self.session_launcher = config.get("session_launcher")
        self.session_users = config.get("session_users", [])

        self.sessions: dict[int, Session] = {}  # slot -> Session
        self._lock = asyncio.Lock()

    async def startup(self):
        """Launch all warm sessions on startup."""
        logger.info(f"[{self.agent_type}] Starting {self.warm_count} warm sessions...")
        for slot in range(1, self.warm_count + 1):
            await self._launch_session(slot, SessionType.WARM)
        logger.info(f"[{self.agent_type}] All warm sessions ready.")
        asyncio.create_task(self._health_loop())

    async def acquire(self) -> Session | None:
        async with self._lock:
            # Find first available session
            for session in self.sessions.values():
                if session.status == SessionStatus.AVAILABLE:
                    session.status = SessionStatus.BUSY
                    logger.info(f"[{self.agent_type}] Acquired slot {session.slot} on port {session.agent_port}")
                    return session

            # No available warm session — spin a dynamic one
            if len(self.sessions) < self.max_sessions:
                slot = self._next_slot()
                logger.info(f"[{self.agent_type}] No warm sessions available, spinning dynamic slot {slot}")
                session = await self._launch_session(slot, SessionType.DYNAMIC)
                if session and session.status == SessionStatus.AVAILABLE:
                    session.status = SessionStatus.BUSY
                    return session

            logger.warning(f"[{self.agent_type}] Max session limit reached, no sessions available.")
            return None

    async def release(self, agent_port: int) -> bool:
        async with self._lock:
            session = self._find_by_port(agent_port)
            if not session:
                return False

            if session.type == SessionType.WARM:
                # Keep warm sessions alive, just mark available
                session.status = SessionStatus.AVAILABLE
                logger.info(f"[{self.agent_type}] Released warm slot {session.slot}")
            else:
                # Tear down dynamic sessions
                await self._kill_session(session)
                del self.sessions[session.slot]
                logger.info(f"[{self.agent_type}] Torn down dynamic slot {session.slot}")

            return True

    def status(self) -> list[dict]:
        return [s.to_dict() for s in self.sessions.values()]

    async def _launch_session(self, slot: int, session_type: SessionType) -> Session | None:
        agent_port = self.agent_base_port + slot
        mcp_port = self.mcp_base_port + slot

        session = Session(
            slot=slot,
            agent_type=self.agent_type,
            host=self.host,
            agent_port=agent_port,
            mcp_port=mcp_port,
            type=session_type,
            status=SessionStatus.STARTING,
        )
        self.sessions[slot] = session

        try:
            
            # Get username for this slot
            username = self.session_users[(slot - 1) % len(self.session_users)]

            # Launch MCP tool
            #session.mcp_process = subprocess.Popen(
            #    [self.mcp_binary, "--urls", f"http://localhost:{mcp_port}"],
            #    stdout=subprocess.DEVNULL,
            #    stderr=subprocess.DEVNULL,
            #)
            subprocess.Popen([
                self.session_launcher,
                "--user", username,
                "--exe", self.agent_script,
                "--args", f"--port {agent_port} --mcp-url http://localhost:{mcp_port}"
            ])

            # Launch agent — pass env vars including MCP URL and OpenAI config
            env = {
                **os.environ,
                "MCP_URL": f"http://localhost:{mcp_port}",
            }

            # agent_dir = os.path.dirname(os.path.abspath(self.agent_script))
            # session.agent_process = subprocess.Popen(
            #     #[self.python_executable, self.agent_script, "--port", str(agent_port)],
            #     [self.agent_script, "--port", str(agent_port)],
            #     cwd=agent_dir,
            #     env=env,
            #     stdout=subprocess.DEVNULL,
            #     stderr=subprocess.DEVNULL,
            # )

            env_str = f"MCP_URL=http://localhost:{mcp_port}"
            subprocess.Popen([
                self.session_launcher,
                "--user", username,
                "--exe", self.agent_script,
                "--args", f"--port {agent_port}"
            ])

            # Wait for agent to be healthy
            await self._wait_for_health(session)
            session.status = SessionStatus.AVAILABLE
            logger.info(f"[{self.agent_type}] Slot {slot} ready — agent:{agent_port} mcp:{mcp_port}")
            return session

        except Exception as e:
            logger.error(f"[{self.agent_type}] Failed to launch slot {slot}: {e}")
            session.status = SessionStatus.DEAD
            return None

    async def _wait_for_health(self, session: Session):
        deadline = asyncio.get_event_loop().time() + self.startup_timeout
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await client.get(f"{session.agent_url}/health", timeout=2)
                    if r.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(1)
        raise TimeoutError(f"[{self.agent_type}] Slot {session.slot} did not become healthy in time.")

    async def _kill_session(self, session: Session):
        for proc in [session.agent_process, session.mcp_process]:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

    async def _health_loop(self):
        """Periodically health check all sessions and restart dead ones."""
        async with httpx.AsyncClient() as client:
            while True:
                await asyncio.sleep(self.health_interval)
                for session in list(self.sessions.values()):
                    if session.status == SessionStatus.BUSY:
                        continue
                    try:
                        r = await client.get(
                            f"{session.agent_url}/health",
                            timeout=self.health_timeout
                        )
                        if r.status_code != 200:
                            raise Exception("Bad status")
                    except Exception:
                        logger.warning(f"[{self.agent_type}] Slot {session.slot} is dead, restarting...")
                        await self._kill_session(session)
                        await self._launch_session(session.slot, session.type)

    def _next_slot(self) -> int:
        used = set(self.sessions.keys())
        for i in range(1, self.max_sessions + 1):
            if i not in used:
                return i
        raise RuntimeError("No slots available")

    def _find_by_port(self, agent_port: int) -> Session | None:
        for s in self.sessions.values():
            if s.agent_port == agent_port:
                return s
        return None


class SessionManager:
    """Top level manager — one AgentPool per agent type."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            config = yaml.safe_load(f)

        global_config = {
            "health_check_interval_seconds": config["health_check_interval_seconds"],
            "health_check_timeout_seconds": config["health_check_timeout_seconds"],
            "startup_timeout_seconds": config["startup_timeout_seconds"],
        }

        self.pools: dict[str, AgentPool] = {
            agent_type: AgentPool(agent_type, agent_config, global_config)
            for agent_type, agent_config in config["agent_types"].items()
        }

    async def startup(self):
        for pool in self.pools.values():
            await pool.startup()

    async def acquire(self, agent_type: str) -> Session | None:
        pool = self.pools.get(agent_type)
        if not pool:
            logger.error(f"Unknown agent type: {agent_type}")
            return None
        return await pool.acquire()

    async def release(self, agent_port: int, agent_type: str) -> bool:
        pool = self.pools.get(agent_type)
        if not pool:
            return False
        return await pool.release(agent_port)

    def status(self) -> dict:
        return {
            agent_type: pool.status()
            for agent_type, pool in self.pools.items()
        }