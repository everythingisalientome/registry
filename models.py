from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import subprocess


class SessionStatus(str, Enum):
    AVAILABLE = "available"
    BUSY = "busy"
    DEAD = "dead"
    STARTING = "starting"


class SessionType(str, Enum):
    WARM = "warm"
    DYNAMIC = "dynamic"


@dataclass
class Session:
    slot: int                          # Slot number (1-10)
    agent_type: str                    # e.g. "desktop", "web"
    host: str
    agent_port: int
    mcp_port: int
    type: SessionType
    status: SessionStatus = SessionStatus.STARTING
    agent_process: Optional[subprocess.Popen] = field(default=None, repr=False)
    mcp_process: Optional[subprocess.Popen] = field(default=None, repr=False)

    @property
    def agent_url(self) -> str:
        return f"http://{self.host}:{self.agent_port}"

    @property
    def mcp_url(self) -> str:
        return f"http://{self.host}:{self.mcp_port}"

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "agent_type": self.agent_type,
            "agent_port": self.agent_port,
            "mcp_port": self.mcp_port,
            "type": self.type,
            "status": self.status,
            "agent_url": self.agent_url,
            "mcp_url": self.mcp_url,
        }