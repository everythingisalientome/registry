import uvicorn
import logging
import argparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from dotenv import load_dotenv
from session_manager import SessionManager

load_dotenv()

logging.basicConfig(level=logging.INFO)

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=str, default="config.yaml")
args = parser.parse_args()

manager = SessionManager(args.config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.startup()
    yield


app = FastAPI(title="Session Registry", lifespan=lifespan)


@app.get("/session/acquire")
async def acquire_session(agent_type: str = Query(..., description="Agent type e.g. 'desktop'")):
    session = await manager.acquire(agent_type)
    if not session:
        raise HTTPException(status_code=503, detail=f"No sessions available for agent type '{agent_type}'.")
    return session.to_dict()


@app.post("/session/release/{agent_port}")
async def release_session(
    agent_port: int,
    agent_type: str = Query(..., description="Agent type e.g. 'desktop'")
):
    success = await manager.release(agent_port, agent_type)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"released": agent_port, "agent_type": agent_type}


@app.get("/session/status")
async def session_status():
    return manager.status()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)