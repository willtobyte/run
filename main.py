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
import websockets
from aiocache.backends.redis import RedisCache
from aiocache.decorators import cached
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

clients = weakref.WeakSet()

client = docker.from_env()
container = client.containers.get("redis")
hostname = container.attrs["Config"]["Hostname"]


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


async def add(connection: ServerConnection) -> None:
    clients.add(connection)
    await broadcast.online(clients)


async def disconnect(connection: ServerConnection) -> None:
    clients.discard(connection)
    await broadcast.online(clients)


async def app(connection: ServerConnection) -> None:
    await add(connection)
    try:
        await broadcast.online(clients)

        async def ping() -> None:
            while True:
                try:
                    await asyncio.sleep(10)
                    await asyncio.wait_for(connection.send(json.dumps({"command": "ping"})), timeout=1)
                except (ConnectionClosed, asyncio.TimeoutError):
                    await disconnect(connection)
                    break

        async def relay() -> None:
            try:
                async for message in connection:
                    match json.loads(message):
                        case {"rpc": {"request": {"id": id, "method": method, "arguments": arguments}}}:
                            response = {"rpc": {"response": {"id": id}}}
                            try:
                                result = await to_thread(partial(import_module(f"procedures.{method}").run, **arguments))  # fmt: skip
                                response["rpc"]["response"]["result"] = result
                            except Exception as exc:
                                response["rpc"]["response"]["error"] = str(exc)
                            await connection.send(json.dumps(response))
                        case _:
                            pass
            except ConnectionClosed:
                await disconnect(connection)

        await asyncio.gather(ping(), relay())
    finally:
        await disconnect(connection)


async def main() -> None:
    async with websockets.serve(app, host="0.0.0.0", port=3000) as server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
