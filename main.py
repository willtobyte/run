import asyncio
import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

clients = set()
lock = asyncio.Lock()
executor = ThreadPoolExecutor(max_workers=os.cpu_count() * 2)  # type: ignore[operator]


async def add(client):
    async with lock:
        clients.add(client)


async def remove(client):
    async with lock:
        clients.remove(client)


async def online(clients: set):
    message = json.dumps({"event": {"topic": "online", "data": {"clients": len(clients)}}})
    await asyncio.gather(*(client.send(message) for client in clients))


broadcast = SimpleNamespace(online=online)


async def app(connection: ServerConnection) -> None:
    await add(connection)
    try:
        await broadcast.online(clients)

        async def ping() -> None:
            while True:
                try:
                    await asyncio.sleep(3)
                    await asyncio.wait_for(connection.send(json.dumps({"command": "ping"})), timeout=1)
                except (ConnectionClosed, asyncio.TimeoutError):
                    await remove(connection)
                    await broadcast.online(clients)
                    break

        async def relay() -> None:
            try:
                async for message in connection:
                    match json.loads(message):
                        case {"rpc": {"request": {"id": id, "method": method, "arguments": arguments}}}:
                            response = {"rpc": {"response": {"id": id}}}
                            try:
                                result = await asyncio.get_running_loop().run_in_executor(
                                    executor,
                                    importlib.import_module(f"procedures.{method}").run,
                                    **arguments,
                                )

                                response["rpc"]["response"]["result"] = result
                            except (ModuleNotFoundError, AttributeError, Exception) as exc:
                                response["rpc"]["response"]["error"] = str(exc)
                            await connection.send(json.dumps(response))
                        case _:
                            pass
            except ConnectionClosed:
                pass

        await asyncio.gather(ping(), relay())
    finally:
        await remove(connection)
        await broadcast.online(clients)


async def main() -> None:
    async with websockets.serve(app, host="0.0.0.0", port=3000) as server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
