"""
ACRE API Gateway
================
FastAPI application exposing:
  - REST API  (POST /analyses, GET /analyses/{id}, ...)
  - GraphQL   (/graphql via Strawberry)
  - WebSocket (/ws/{analysis_id}) — real-time agent progress
  - Auth      (JWT bearer tokens + API key header)
  - Metrics   (/metrics for Prometheus scraping)
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import strawberry
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from prometheus_fastapi_instrumentator import Instrumentator
from strawberry.fastapi import GraphQLRouter

from db import get_db, init_db
from routers import analyses, repos, patches, webhooks
from gql.schema import Query, Mutation
from auth import verify_token, verify_api_key

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

redis_client: aioredis.Redis = None

# WebSocket connection manager — maps analysis_id → set of WebSocket connections
class ConnectionManager:
    def __init__(self):
        self.active: dict[str, set[WebSocket]] = {}

    async def connect(self, analysis_id: str, ws: WebSocket):
        await ws.accept()
        self.active.setdefault(analysis_id, set()).add(ws)
        logger.info(f"WS connected: {analysis_id} ({len(self.active[analysis_id])} clients)")

    def disconnect(self, analysis_id: str, ws: WebSocket):
        if analysis_id in self.active:
            self.active[analysis_id].discard(ws)

    async def broadcast(self, analysis_id: str, message: dict):
        clients = self.active.get(analysis_id, set())
        dead = set()
        for ws in clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            clients.discard(ws)


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    await init_db(os.getenv("POSTGRES_URL"))

    # Subscribe to Redis pub/sub and relay events to WebSocket clients
    import asyncio
    asyncio.create_task(_redis_relay())

    yield
    await redis_client.close()


async def _redis_relay():
    """
    Background task: subscribes to all acre:events:* channels on Redis
    and relays messages to connected WebSocket clients.
    """
    pubsub = redis_client.pubsub()
    await pubsub.psubscribe("acre:events:*")
    async for message in pubsub.listen():
        if message["type"] != "pmessage":
            continue
        channel: str = message["channel"].decode()
        analysis_id = channel.split(":", 2)[-1]
        try:
            data = json.loads(message["data"])
            await manager.broadcast(analysis_id, data)
        except Exception:
            pass


# ── GraphQL Schema ────────────────────────────────────────────────────────────
schema = strawberry.Schema(query=Query, mutation=Mutation)
graphql_router = GraphQLRouter(schema)


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="ACRE — Autonomous Codebase Reliability Engineer",
    version="1.0.0",
    description="AI-powered code analysis, bug detection, and automated patching",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus metrics
Instrumentator().instrument(app).expose(app)

# Routers
app.include_router(analyses.router, prefix="/api/v1/analyses", tags=["analyses"])
app.include_router(repos.router,    prefix="/api/v1/repos",    tags=["repos"])
app.include_router(patches.router,  prefix="/api/v1/patches",  tags=["patches"])
app.include_router(webhooks.router, prefix="/webhooks",        tags=["webhooks"])
app.include_router(graphql_router,  prefix="/graphql")


# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws/{analysis_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    analysis_id: str,
    token: str | None = None,
):
    """
    Real-time analysis progress stream.
    Client connects, receives events as agents run.
    Event shape: { event: string, status: string, ...payload }
    """
    await manager.connect(analysis_id, websocket)

    # Send current status immediately on connect
    status_key = f"acre:ingestion:{analysis_id}"
    current = await redis_client.hgetall(status_key)
    if current:
        await websocket.send_json({
            "event": "current_status",
            **{k.decode(): v.decode() for k, v in current.items()}
        })

    try:
        while True:
            # Keep alive — client can also send pings
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"event": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(analysis_id, websocket)
        logger.info(f"WS disconnected: {analysis_id}")


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/", tags=["health"])
async def root():
    return {
        "name": "ACRE API",
        "docs": "/docs",
        "graphql": "/graphql",
        "metrics": "/metrics",
    }
