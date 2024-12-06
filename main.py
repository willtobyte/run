import asyncio
import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

clients = set()
executor = ThreadPoolExecutor(max_workers=os.cpu_count() * 2)


async def broadcast_online():
    message = json.dumps({"event": {"topic": "online", "data": {"clients": len(clients)}}})
    await asyncio.gather(*(client.send(message) for client in clients))


async def app(connection: ServerConnection) -> None:
    clients.add(connection)
    try:
        await broadcast_online()

        async def ping() -> None:
            while True:
                try:
                    await asyncio.sleep(3)
                    await connection.send(json.dumps({"command": "ping"}))
                except ConnectionClosed:
                    break

        async def relay() -> None:
            try:
                async for message in connection:
                    match json.loads(message):
                        case {"rpc": {"request": {"id": id, "method": method, "arguments": arguments}}}:
                            response = {"rpc": {"response": {"id": id}}}
                            try:
                                module = importlib.import_module(f"procedures.{method}")
                                result = await asyncio.get_running_loop().run_in_executor(
                                    executor,
                                    module.run,
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
        clients.remove(connection)
        await broadcast_online()


async def main() -> None:
    async with websockets.serve(app, host="0.0.0.0", port=3000) as server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
