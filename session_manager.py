import asyncio
import logging
import os
import subprocess
import httpx
import yaml
from models import Session, SessionStatus, SessionType

logger = logging.getLogger(__name__)


class AgentPool:
    """Manages sessions for a single agent type."""

    def __init__(self, agent_type: str, config: dict, global_config: dict,
                 registry_host: str, session_service_url: str):
        self.agent_type          = agent_type
        self.warm_count          = config["warm_sessions"]   # fix: was min_sessions
        self.max_sessions        = config["max_sessions"]
        self.agent_base_port     = config["agent_base_port"]
        self.mcp_base_port       = config["mcp_base_port"]
        self.agent_script        = config["agent_script"]
        self.mcp_binary          = config["mcp_binary"]
        self.host                = registry_host
        self.session_mode        = config.get("session_mode", "local")
        self.session_service_url = session_service_url
        self.startup_timeout     = global_config["startup_timeout_seconds"]
        self.health_interval     = global_config["health_check_interval_seconds"]
        self.health_timeout      = global_config["health_check_timeout_seconds"]

        # local mode only
        self.sessions: dict[int, Session] = {}
        self._lock = asyncio.Lock()

    async def startup(self):
        logger.info(f"[{self.agent_type}] Mode: {self.session_mode} — "
                    f"Warming {self.warm_count} sessions...")

        if self.session_mode == "rdsh":
            await self._startup_rdsh()
        else:
            for slot in range(1, self.warm_count + 1):
                await self._launch_local_session(slot, SessionType.WARM)
                await asyncio.sleep(5)

        logger.info(f"[{self.agent_type}] All warm sessions ready.")
        asyncio.create_task(self._health_loop())

    # ── RDSH mode ─────────────────────────────────────────────────────────────

    async def _startup_rdsh(self):
        """
        Ask SessionService to inject agent + MCP into warm_count available sessions.
        SessionService already has the RDP sessions running — we just request injection.
        """
        async with httpx.AsyncClient() as client:
            for i in range(self.warm_count):
                slot       = i + 1
                agent_port = self.agent_base_port + slot
                mcp_port   = self.mcp_base_port   + slot

                logger.info(f"[{self.agent_type}] Requesting injection for slot {slot}...")

                try:
                    # Ask SessionService to inject agent + MCP into an available session
                    resp = await client.post(
                        f"{self.session_service_url}/sessions/inject-next",
                        json={
                            "agent_type":   self.agent_type,
                            "agent_script": self.agent_script,
                            "mcp_binary":   self.mcp_binary,
                            "agent_port":   agent_port,
                            "mcp_port":     mcp_port,
                        },
                        timeout=60,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    logger.info(f"[{self.agent_type}] Slot {slot} injected into "
                                f"Windows session {data.get('windows_session_id')}")

                    # Wait for agent health
                    await self._wait_for_agent_health(agent_port)
                    logger.info(f"[{self.agent_type}] Slot {slot} healthy — "
                                f"agent:{agent_port} mcp:{mcp_port}")

                except Exception as e:
                    logger.error(f"[{self.agent_type}] Failed to warm slot {slot}: {e}")

                await asyncio.sleep(3)

    async def acquire(self) -> Session | None:
        if self.session_mode == "rdsh":
            return await self._acquire_rdsh()
        else:
            return await self._acquire_local()

    async def _acquire_rdsh(self) -> Session | None:
        """Ask SessionService for an available session."""
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    f"{self.session_service_url}/sessions/available",
                    params={"type": self.agent_type},
                    timeout=10,
                )
                if resp.status_code == 503:
                    logger.warning(f"[{self.agent_type}] No sessions available in SessionService")
                    return None
                resp.raise_for_status()
                data = resp.json()

                return Session(
                    slot       = data["session_id"],
                    agent_type = self.agent_type,
                    host       = data["host"],
                    agent_port = data["agent_port"],
                    mcp_port   = data["mcp_port"],
                    type       = SessionType.WARM,
                    status     = SessionStatus.BUSY,
                )
            except Exception as e:
                logger.error(f"[{self.agent_type}] acquire_rdsh failed: {e}")
                return None

    async def release(self, agent_port: int) -> bool:
        if self.session_mode == "rdsh":
            return await self._release_rdsh(agent_port)
        else:
            return await self._release_local(agent_port)

    async def _release_rdsh(self, agent_port: int) -> bool:
        """Tell SessionService to release the session and re-inject fresh agent."""
        slot = agent_port - self.agent_base_port
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.session_service_url}/sessions/{slot}/release",
                    timeout=60,
                )
                resp.raise_for_status()
                logger.info(f"[{self.agent_type}] Released session slot {slot}")
                return True
            except Exception as e:
                logger.error(f"[{self.agent_type}] release_rdsh failed: {e}")
                return False

    # ── Local mode (unchanged) ────────────────────────────────────────────────

    async def _acquire_local(self) -> Session | None:
        async with self._lock:
            for session in self.sessions.values():
                if session.status == SessionStatus.AVAILABLE:
                    session.status = SessionStatus.BUSY
                    logger.info(f"[{self.agent_type}] Acquired slot {session.slot} "
                                f"on port {session.agent_port}")
                    return session

            if len(self.sessions) < self.max_sessions:
                slot = self._next_slot()
                logger.info(f"[{self.agent_type}] No warm sessions, spinning dynamic slot {slot}")
                session = await self._launch_local_session(slot, SessionType.DYNAMIC)
                if session and session.status == SessionStatus.AVAILABLE:
                    session.status = SessionStatus.BUSY
                    return session

            logger.warning(f"[{self.agent_type}] Max session limit reached.")
            return None

    async def _release_local(self, agent_port: int) -> bool:
        async with self._lock:
            session = self._find_by_port(agent_port)
            if not session:
                return False
            if session.type == SessionType.WARM:
                session.status = SessionStatus.AVAILABLE
                logger.info(f"[{self.agent_type}] Released warm slot {session.slot}")
            else:
                await self._kill_session(session)
                del self.sessions[session.slot]
                logger.info(f"[{self.agent_type}] Torn down dynamic slot {session.slot}")
            return True

    async def _launch_local_session(self, slot: int,
                                     session_type: SessionType) -> Session | None:
        agent_port = self.agent_base_port + slot
        mcp_port   = self.mcp_base_port   + slot
        agent_dir  = os.path.dirname(os.path.abspath(self.agent_script))

        session = Session(
            slot       = slot,
            agent_type = self.agent_type,
            host       = self.host,
            agent_port = agent_port,
            mcp_port   = mcp_port,
            type       = session_type,
            status     = SessionStatus.STARTING,
        )
        self.sessions[slot] = session

        try:
            session.mcp_process = subprocess.Popen(
                [self.mcp_binary, "--urls", f"http://localhost:{mcp_port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            env        = {**os.environ, "MCP_URL": f"http://localhost:{mcp_port}"}
            stderr_log = open(
                os.path.join(agent_dir, f"agent_stderr_{agent_port}.log"), "w")

            session.agent_process = subprocess.Popen(
                [self.agent_script, "--port", str(agent_port),
                 "--mcp-url", f"http://localhost:{mcp_port}"],
                cwd    = agent_dir,
                env    = env,
                stdout = subprocess.DEVNULL,
                stderr = stderr_log,
            )

            await self._wait_for_agent_health(agent_port)
            session.status = SessionStatus.AVAILABLE
            logger.info(f"[{self.agent_type}] Slot {slot} ready — "
                        f"agent:{agent_port} mcp:{mcp_port}")
            return session

        except Exception as e:
            logger.error(f"[{self.agent_type}] Failed to launch slot {slot}: {e}")
            session.status = SessionStatus.DEAD
            return None

    async def _wait_for_agent_health(self, agent_port: int):
        deadline    = asyncio.get_event_loop().time() + self.startup_timeout
        health_url  = f"http://localhost:{agent_port}/health"
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await client.get(health_url, timeout=2)
                    if r.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(1)
        raise TimeoutError(f"Agent on port {agent_port} did not become healthy in time.")

    async def _kill_session(self, session: Session):
        for proc in [session.agent_process, session.mcp_process]:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

    async def _health_loop(self):
        async with httpx.AsyncClient() as client:
            while True:
                await asyncio.sleep(self.health_interval)

                if self.session_mode == "rdsh":
                    # SessionService owns health monitoring for rdsh sessions
                    continue

                for session in list(self.sessions.values()):
                    if session.status == SessionStatus.BUSY:
                        continue
                    try:
                        r = await client.get(
                            f"http://localhost:{session.agent_port}/health",
                            timeout=self.health_timeout,
                        )
                        if r.status_code != 200:
                            raise Exception("Bad status")
                    except Exception as e:
                        logger.warning(f"[{self.agent_type}] Slot {session.slot} "
                                       f"is dead ({e}), restarting...")
                        await self._kill_session(session)
                        await self._launch_local_session(session.slot, session.type)

    def status(self) -> list[dict]:
        return [s.to_dict() for s in self.sessions.values()]

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
            "health_check_timeout_seconds":  config["health_check_timeout_seconds"],
            "startup_timeout_seconds":       config["startup_timeout_seconds"],
        }

        registry_host        = config.get("registry", {}).get("host", "localhost")
        ss_host              = config.get("session_service", {}).get("host", "localhost")
        ss_port              = config.get("session_service", {}).get("port", 9001)
        session_service_url  = f"http://{ss_host}:{ss_port}"

        self.pools: dict[str, AgentPool] = {
            agent_type: AgentPool(
                agent_type, agent_config, global_config,
                registry_host, session_service_url,
            )
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