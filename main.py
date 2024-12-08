import asyncio
import hashlib
import json
import weakref
from asyncio import to_thread
from datetime import timedelta
from functools import partial
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace

import docker
import httpx
from aiocache.backends.redis import RedisCache
from aiocache.decorators import cached
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic import ValidationError

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

clients = weakref.WeakSet()

client = docker.from_env()
container = client.containers.get("redis")
hostname = container.attrs["Config"]["Hostname"]


class URLBaseParams(BaseModel):
    runtime: str
    org: str
    repo: str
    release: str
    format: str
    remaining: str = ""


@app.middleware("http")
async def extract_arguments(request: Request, call_next):
    try:
        parts = request.url.path.strip("/").split("/")
        request.scope["arguments"] = URLBaseParams(
            runtime=parts[0],
            org=parts[1],
            repo=parts[2],
            release=parts[3],
            format=parts[4],
            remaining="/".join(parts[5:]),
        ).dict()
    except (IndexError, ValueError, ValidationError):
        raise HTTPException(status_code=400, detail="Invalid URL structure or missing keys")
    return await call_next(request)
c

@cached(ttl=timedelta(days=365).total_seconds(), cache=RedisCache, endpoint=hostname)
async def fetch(url: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            hasher = hashlib.sha1()
            buffer = BytesIO()
            async for chunk in response.aiter_bytes():
                buffer.write(chunk)
                hasher.update(chunk)
            buffer.seek(0)
            return buffer.getvalue(), hasher.hexdigest()


async def online(clients: set) -> None:
    message = json.dumps({"event": {"topic": "online", "data": {"clients": len(clients)}}})
    await asyncio.gather(*(client.send(message) for client in clients))


broadcast = SimpleNamespace(online=online)


async def add(connection: WebSocket) -> None:
    clients.add(connection)
    await broadcast.online(clients)


async def disconnect(connection: WebSocket) -> None:
    clients.discard(connection)
    await broadcast.online(clients)


@app.websocket("/socket")
async def websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    clients.add(websocket)

    try:

        async def ping() -> None:
            while True:
                try:
                    await asyncio.sleep(10)
                    await websocket.send_text(json.dumps({"command": "ping"}))
                except (WebSocketDisconnect, asyncio.TimeoutError):
                    await disconnect(websocket)
                    break

        async def relay() -> None:
            try:
                async for message in websocket.iter_text():
                    match json.loads(message):
                        case {"rpc": {"request": {"id": id, "method": method, "arguments": arguments}}}:
                            response = {"rpc": {"response": {"id": id}}}
                            try:
                                module = import_module(f"procedures.{method}")
                                func = partial(module.run, **arguments)
                                result = await to_thread(func)
                                response["rpc"]["response"]["result"] = result
                            except Exception as exc:
                                response["rpc"]["response"]["error"] = str(exc)
                            await websocket.send_text(json.dumps(response))
                        case _:
                            pass
            except WebSocketDisconnect:
                await disconnect(websocket)

        await asyncio.gather(ping(), relay())
    finally:
        await disconnect(websocket)
