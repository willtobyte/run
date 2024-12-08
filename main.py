import asyncio
import hashlib
import json
import os
import weakref
import zipfile
from asyncio import to_thread
from datetime import timedelta
from functools import partial
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlparse

import docker
import httpx
from aiocache.backends.redis import RedisCache
from aiocache.decorators import cached
from aiocache.serializers import PickleSerializer
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic import ValidationError
from redis.asyncio import Redis

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

clients = weakref.WeakSet()


class Metadata(BaseModel):
    runtime: str
    organization: str
    repository: str
    release: str
    format: str
    remaining: str = ""


@app.middleware("http")
async def metadata(request: Request, call_next):
    try:
        parts = request.url.path.strip("/").split("/")
        request.scope["metadata"] = Metadata(
            runtime=parts[0],
            organization=parts[1],
            repository=parts[2],
            release=parts[3],
            format=parts[4],
            remaining="/".join(parts[5:]),
        ).dict()
    except (IndexError, ValueError, ValidationError):
        # raise HTTPException(status_code=400, detail="Invalid URL structure or missing keys")
        pass
    return await call_next(request)


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


# url := fmt.Sprintf("https://github.com/flippingpixels/carimbo/releases/download/v%s/WebAssembly.zip", runtime)


redis = None
client = docker.from_env()
container = client.containers.get("redis")
hostname = container.attrs["Config"]["Hostname"]


@app.on_event("startup")
async def startup_event():
    global redis
    redis = Redis(host=hostname, port=6379, decode_responses=False)
    await redis.ping()


@app.on_event("shutdown")
async def shutdown_event():
    global redis
    if redis:
        await redis.close()


async def get_redis() -> Redis:
    if not redis:
        raise RuntimeError("Redis client is not initialized.")
    return redis


async def download(
    redis: Redis,
    url: str,
    filename: str,
    ttl: timedelta = timedelta(hours=1),
) -> tuple[bytes, str] | None:
    namespace = url.split("://", 1)[-1]

    async with redis.pipeline(transaction=True) as pipe:
        pipe.get(f"{namespace}:{filename}:content")
        pipe.get(f"{namespace}:{filename}:hash")
        data, hash = await pipe.execute()

    match data, hash:
        case (bytes() as data, bytes() as hash) if all(value and value.strip() for value in (data, hash)):
            return data, hash.decode()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url)
        ext = os.path.splitext(urlparse(url).path)[-1].lower()

        async with redis.pipeline(transaction=True) as pipe:
            match ext:
                case ".zip":
                    with zipfile.ZipFile(BytesIO(response.content)) as zf:
                        result = None
                        for name in zf.namelist():
                            content = zf.read(name)
                            hash = hashlib.sha1(content).hexdigest()
                            pipe.set(f"{namespace}:{name}:hash", hash, ex=int(ttl.total_seconds()))
                            pipe.set(f"{namespace}:{name}:content", content, ex=int(ttl.total_seconds()))
                            if name == filename:
                                result = (content, hash)

                        await pipe.execute()

                        return result

                case _:
                    data = response.content
                    hash = hashlib.sha1(data).hexdigest()

                    pipe.set(f"{namespace}:{filename}:hash", hash, ex=int(ttl.total_seconds()))
                    pipe.set(f"{namespace}:{filename}:content", data, ex=int(ttl.total_seconds()))
                    await pipe.execute()

                    return data, hash


@cached(ttl=timedelta(hours=1).total_seconds(), serializer=PickleSerializer(), cache=RedisCache, endpoint=hostname)
async def fetch(url: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            hasher = hashlib.sha1()
            buffer = BytesIO()
            async for chunk in response.aiter_bytes():
                buffer.write(chunk)
                hasher.update(chunk)
            buffer.seek(0)
            return buffer.getvalue(), hasher.hexdigest()


@app.get("/")
async def read_root(redis: Redis = Depends(get_redis)):
    runtime = "1.0.16"
    url = f"https://github.com/flippingpixels/carimbo/releases/download/v{runtime}/WebAssembly.zip"

    result = await download(redis, url, "carimbo.js")
    if result is None:
        return {"ok": False}

    [file_bytes, sha1_hex] = result
    return {"ok": True, "sha1_hex": sha1_hex}


# async def download():
#     runtime = "1.0.16"

#     url = f"https://github.com/flippingpixels/carimbo/releases/download/v{runtime}/WebAssembly.zip"

#     [b, h] = await fetch(url)
#     print(f"Hex {h}")
