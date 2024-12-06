import asyncio
import importlib
import json

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed


async def app(connection: ServerConnection) -> None:
    async def ping() -> None:
        while True:
            try:
                await asyncio.sleep(3)
                await connection.send(json.dumps({"command": "ping"}))
                await connection.send(
                    json.dumps({"event": {"topic": "myevent", "data": {"foobar": "abc123", "arr": [1, 2, 3]}}})
                )
            except ConnectionClosed:
                break

    async def echo() -> None:
        try:
            async for message in connection:
                match json.loads(message):
                    case {"rpc": {"request": {"id": id, "method": method, "arguments": arguments}}}:
                        response = {"rpc": {"response": {"id": id}}}
                        try:
                            module = importlib.import_module(f"procedures.{method}")
                            result = module.run(arguments)
                            response["rpc"]["response"]["result"] = result
                        except (ModuleNotFoundError, AttributeError, Exception) as exc:
                          response["rpc"]["response"]["error"] = str(exc)
                        await connection.send(json.dumps(response))
                    case _:
                        pass
        except ConnectionClosed:
            pass

    await asyncio.gather(ping(), echo())


async def main() -> None:
    async with websockets.serve(app, host="0.0.0.0", port=3000) as server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
