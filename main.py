import asyncio
import base64
import hashlib
import logging
import os
import zipfile
from asyncio import to_thread
from datetime import datetime
from datetime import timedelta
from importlib import import_module
from io import BytesIO
from time import mktime
from types import SimpleNamespace
from typing import AsyncGenerator
from typing import Union
from urllib.parse import urlparse
from wsgiref.handlers import format_date_time

import docker
import httpx
import yaml
from fastapi import APIRouter
from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi import status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown import markdown
from redis.asyncio import Redis
from tenacity import retry
from tenacity import stop_after_attempt
from tenacity import wait_fixed

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/media", StaticFiles(directory="media"), name="media")


class Context:
    def __init__(self, clients: set[WebSocket], lock: asyncio.Lock) -> None:
        self.clients = clients
        self.lock = lock


clients = set()
lock = asyncio.Lock()
context = Context(clients, lock)


with open("database.yaml", "rt") as f:
    database = yaml.safe_load(f)


async def online(clients: set) -> None:
    message = {"event": {"topic": "online", "data": {"clients": len(clients)}}}
    tasks = (asyncio.create_task(c.send_json(message)) for c in clients)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failed = {c for c, r in zip(clients, results) if isinstance(r, Exception)}
    clients.difference_update(failed)


broadcast = SimpleNamespace(online=online)


async def add(websocket: WebSocket) -> None:
    async with lock:
        clients.add(websocket)
        logger.info(f"Client connected. Total clients: {len(clients)}")
        await broadcast.online(clients)


async def disconnect(websocket: WebSocket) -> None:
    async with lock:
        clients.discard(websocket)
        logger.info(f"Client disconnected. Total clients: {len(clients)}")
        await broadcast.online(clients)


@app.get("/favicon.ico")
async def favicon():
    return Response(content=b"", media_type="image/x-icon")


@app.websocket("/socket")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    await add(websocket)

    try:

        async def heartbeat() -> None:
            while True:
                try:
                    await asyncio.sleep(10)
                    await websocket.send_json({"command": "ping"})
                except (WebSocketDisconnect, asyncio.TimeoutError):
                    break

        async def relay() -> None:
            try:
                async for message in websocket.iter_json():
                    match message:
                        case {"rpc": {"request": {"id": id, "method": method, "arguments": arguments}}}:
                            response = {"rpc": {"response": {"id": id}}}
                            try:
                                dispatch = {list: lambda args: module.run(args), dict: lambda args: module.run(**args)}
                                module = import_module(f"procedures.{method}")
                                result = await to_thread(dispatch[type(arguments)], arguments)
                                response["rpc"]["response"]["result"] = result

                                logger.info(
                                    f"Successfully executed {method} with arguments: {arguments} and result: {result}"
                                )
                            except Exception as exc:
                                logger.error(
                                    f"Error executing {method} with arguments {arguments}: {exc}",
                                    exc_info=True,
                                )
                                response["rpc"]["response"]["error"] = str(exc)

                            await websocket.send_json(response)
                        case _:
                            pass
            except WebSocketDisconnect:
                pass

        tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(relay())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    finally:
        await disconnect(websocket)


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


async def stream_content(content: bytes) -> AsyncGenerator[bytes, None]:
    yield content


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
async def download(
    redis: Redis,
    url: str,
    filename: str,
    ttl: timedelta = timedelta(days=365),
) -> tuple[AsyncGenerator[bytes, None], str] | None:
    namespace = url.split("://", 1)[-1]
    content_key = f"{namespace}:{filename}:content"
    hash_key = f"{namespace}:{filename}:hash"

    cached_content, cached_hash = await redis.mget([content_key, hash_key])
    if (
        isinstance(cached_content, bytes)
        and isinstance(cached_hash, bytes)
        and cached_content.strip()
        and cached_hash.strip()
    ):
        logger.info(f"Cache hit for resource {url} with hash {cached_hash.decode()}")
        return stream_content(cached_content), cached_hash.decode()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url)
        logger.info(f"Request to {url} returned status code {response.status_code}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Resource not found")
            else:
                raise

    parsed_path = urlparse(url).path
    ext = os.path.splitext(parsed_path)[-1].lower()

    if ext == ".zip":

        def decompress() -> tuple[tuple[AsyncGenerator[bytes, None], str] | None, list[tuple[str, Union[bytes, str]]]]:
            commands: list[tuple[str, Union[bytes, str]]] = []
            result: tuple[AsyncGenerator[bytes, None], str] | None = None
            with zipfile.ZipFile(BytesIO(response.content)) as zf:
                for name in zf.namelist():
                    file_content = zf.read(name)
                    file_hash = base64.b64encode(hashlib.sha256(file_content).digest()).decode()
                    content_key = f"{namespace}:{name}:content"
                    hash_key = f"{namespace}:{name}:hash"
                    commands.append((hash_key, file_hash))
                    commands.append((content_key, file_content))
                    if name == filename:
                        result = (stream_content(file_content), file_hash)
            return result, commands

        result, commands = await asyncio.to_thread(decompress)
        async with redis.pipeline(transaction=True) as pipe:
            for key, value in commands:
                pipe.set(key, value, ex=ttl)
            await pipe.execute()
        return result

    data = response.content
    content_hash = base64.b64encode(hashlib.sha256(data).digest()).decode()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(hash_key, content_hash, ex=ttl)
        pipe.set(content_key, data, ex=ttl)
        await pipe.execute()
    return stream_content(data), content_hash


@app.head("/")
async def healthcheck(redis: Redis = Depends(get_redis)):
    await redis.ping()
    return Response(status_code=status.HTTP_200_OK)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, response_class=HTMLResponse):
    artifacts = database.get("artifacts", [])
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"artifacts": artifacts},
    )


router = APIRouter()


@router.get("/play/{slug}", response_class=HTMLResponse)
async def play(slug: str, request: Request):
    try:
        artifact = next(a for a in database["artifacts"] if a["slug"] == slug)
    except StopIteration:
        raise HTTPException(status_code=404, detail="Artifact not found")

    about = markdown(artifact["about"])
    mapping = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}
    width, height = mapping[artifact["resolution"]]

    context = artifact | {
        "about": about,
        "url": f"/play/{slug}/",
        "width": width,
        "height": height,
    }
    return templates.TemplateResponse(request=request, name="play.html", context=context)


@router.get("/assets/{runtime}/{organization}/{repository}/{release}/{filename}")
async def dynamic(
    runtime: str,
    organization: str,
    repository: str,
    release: str,
    filename: str,
    redis: Redis = Depends(get_redis),
):
    match filename:
        case "bundle.7z":
            url = f"https://github.com/{organization}/{repository}/releases/download/v{release}/bundle.7z"
            media_type = "application/x-7z-compressed"
        case "carimbo.js":
            url = f"https://github.com/willtobyte/carimbo/releases/download/v{runtime}/WebAssembly.zip"
            media_type = "application/javascript"
        case "carimbo.wasm":
            url = f"https://github.com/willtobyte/carimbo/releases/download/v{runtime}/WebAssembly.zip"
            media_type = "application/wasm"
        case _:
            raise HTTPException(status_code=404)

    result = await download(redis, url, filename)
    if result is None:
        raise HTTPException(status_code=404)

    content, etag = result
    now = datetime.utcnow()
    timestamp = mktime(now.timetuple())
    modified = format_date_time(timestamp)
    duration = timedelta(days=365).total_seconds()
    headers = {
        "Cache-Control": f"public, max-age={int(duration)}, immutable",
        "Content-Disposition": f'inline; filename="{filename}"',
        "Last-Modified": modified,
        "ETag": etag,
    }

    return StreamingResponse(
        content=content,
        media_type=media_type,
        headers=headers,
    )


app.include_router(router)
