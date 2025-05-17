import asyncio
import io
import logging
import os
from asyncio import to_thread
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import Awaitable
from typing import Callable

import docker
import py7zr
from fastapi import APIRouter
from fastapi import FastAPI
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis
from rx import create
from rx.core.typing import Observable
from rx.core.typing import Observer
from rx.disposable import CompositeDisposable
from rx.operators import debounce
from rx.operators import flat_map
from rx.scheduler.eventloop import AsyncIOScheduler
from rx.subject import Subject
from starlette.responses import Response
from starlette.types import Scope
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer as WatchdogObserver

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(GZipMiddleware, minimum_size=728)

templates = Jinja2Templates(directory="templates")


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


async def get_redis() -> Redis:
    if not redis:
        raise RuntimeError("Redis client is not initialized.")
    return redis


async def reload() -> None:
    message: dict[str, Any] = {"command": "reload"}
    tasks = (asyncio.create_task(c.send_json(message)) for c in clients)
    await asyncio.gather(*tasks)


class FilteredEventHandler(PatternMatchingEventHandler):
    def __init__(self, notifier: Subject) -> None:
        super().__init__(patterns=["*.js", "*.wasm"], ignore_directories=True)
        self.notifier: Subject = notifier

    def on_modified(self, event: FileSystemEvent) -> None:
        self.notifier.on_next(0)


class AllFilesEventHandler(FileSystemEventHandler):
    def __init__(self, notifier: Subject) -> None:
        self.notifier: Subject = notifier

    def on_modified(self, event: FileSystemEvent) -> None:
        self.notifier.on_next(0)


def observe(path: str, handler: FileSystemEventHandler) -> WatchdogObserver:
    observer: WatchdogObserver = WatchdogObserver()
    observer.schedule(handler, path, recursive=True)
    observer.start()
    return observer


@app.on_event("startup")
async def startup_event() -> None:
    global redis
    redis = Redis(host=hostname, port=6379, decode_responses=False)
    await redis.ping()

    def execute(coroutine: Callable[[], Awaitable[Any]]) -> Observable:
        def observable(observer: Observer, _: Any) -> None:
            async def run() -> None:
                try:
                    result: Any = await coroutine()
                    observer.on_next(result)
                    observer.on_completed()
                except Exception as ex:
                    observer.on_error(ex)

            asyncio.create_task(run())

        return create(observable)

    loop = asyncio.get_event_loop()
    scheduler = AsyncIOScheduler(loop=loop)

    engine_notifier: Subject = Subject()
    engine_subscription = engine_notifier.pipe(
        debounce(3.0, scheduler=scheduler), flat_map(lambda _: execute(reload))
    ).subscribe()
    engine_handler = FilteredEventHandler(engine_notifier)
    engine_observer = observe("/opt/engine", engine_handler)

    game_notifier: Subject = Subject()
    game_subscription = game_notifier.pipe(
        debounce(3.0, scheduler=scheduler), flat_map(lambda _: execute(reload))
    ).subscribe()
    game_handler = AllFilesEventHandler(game_notifier)
    game_observer = observe("/opt/game", game_handler)

    app.state.observers = [engine_observer, game_observer]
    app.state.subscriptions = CompositeDisposable(engine_subscription, game_subscription)
    app.state.notifiers = [engine_notifier, game_notifier]


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global redis
    if redis:
        await redis.close()

    if hasattr(app.state, "observers"):
        for observer in app.state.observers:
            observer.stop()
            observer.join()
    if hasattr(app.state, "subscriptions"):
        app.state.subscriptions.dispose()
    if hasattr(app.state, "notifiers"):
        for notifier in app.state.notifiers:
            notifier.on_completed()


playground = APIRouter(prefix="/playground")


@playground.get("/")
async def debug(request: Request) -> Response:
    return templates.TemplateResponse("playground.html", context={"request": request})


@playground.get("/bundle.7z")
async def bundle() -> StreamingResponse:
    source: str = "/opt/game"

    def generate() -> Any:
        with io.BytesIO() as buffer:
            with py7zr.SevenZipFile(
                buffer,
                mode="w",
                filters=[
                    {
                        "id": py7zr.FILTER_LZMA2,
                        "preset": 2,
                    }
                ],
            ) as archive:
                for root, dirs, files in os.walk(source):
                    if ".git" in dirs:
                        dirs.remove(".git")
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, source)
                        archive.write(file_path, arcname)
            buffer.seek(0)
            while chunk := buffer.read(8192 * 64):
                yield chunk

    return StreamingResponse(
        content=generate(),
        media_type="application/x-7z-compressed",
        headers={
            "Content-Disposition": "attachment; filename=bundle.7z",
            "Cache-Control": "no-transform",
        },
    )


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        cleaned_path = str(Path(path.split("@")[0]))
        response = await super().get_response(cleaned_path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response


app.include_router(playground)
app.mount("/src", NoCacheStaticFiles(directory="/opt/src"), name="assets")
app.mount("/playground", NoCacheStaticFiles(directory="/opt/engine"), name="assets")
