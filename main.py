import asyncio
import logging
from asyncio import to_thread
from importlib import import_module
from types import SimpleNamespace

import docker
from fastapi import FastAPI
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Context:
    def __init__(self, clients: set[WebSocket], lock: asyncio.Lock) -> None:
        self.clients = clients
        self.lock = lock


clients = set()
lock = asyncio.Lock()
context = Context(clients, lock)


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


@app.websocket("/")
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
