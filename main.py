import asyncio
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
                        print(f"{method=} {arguments=}")
                        response = json.dumps({"rpc": {"response": {"id": id, "result": {"foofoo": True}}}})
                        await connection.send(response)
                    case _:
                        pass
        except ConnectionClosed:
            pass

    await asyncio.gather(ping(), echo())


async def main() -> None:
    async with websockets.serve(app, "localhost", 3000) as server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
