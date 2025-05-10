import asyncio
import io
import os
import shutil
import zipfile
from itertools import chain
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple

import aiofiles
import httpx
import yaml
from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import Template
from markdown import markdown

environ: Environment = Environment(loader=FileSystemLoader("templates"), autoescape=True, enable_async=True)

environ.filters["markdown"] = markdown

with open("database.yaml", "rt") as f:
    database: Dict[str, Any] = yaml.safe_load(f)  # type: ignore


async def download_runtime(
    runtime: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        buffer: io.BytesIO = io.BytesIO()
        url: str = f"https://github.com/willtobyte/carimbo/releases/download/v{runtime}/WebAssembly.zip"
        async with client.stream(
            "GET",
            url,
            headers={"Authorization": f"token {os.environ['GITHUB_TOKEN']}"},
        ) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():  # type: bytes
                buffer.write(chunk)

    buffer.seek(0)
    output: Path = Path("output/runtimes") / runtime
    output.mkdir(parents=True, exist_ok=True)

    def extract() -> None:
        with zipfile.ZipFile(buffer) as zipf:
            zipf.extractall(path=output)

    await asyncio.to_thread(extract)


async def download_bundle(
    repository: str,
    release: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> None:
    url: str = f"https://github.com/{repository}/releases/download/v{release}/bundle.7z"

    output: Path = Path("output/bundles") / repository / release
    output.mkdir(parents=True, exist_ok=True)
    bundle: Path = output / "bundle.7z"

    async with semaphore:
        async with client.stream(
            "GET",
            url,
            headers={"Authorization": f"token {os.environ['GITHUB_TOKEN']}"},
        ) as r:
            r.raise_for_status()
            async with aiofiles.open(bundle, "wb") as f:
                async for chunk in r.aiter_bytes():
                    await f.write(chunk)


async def render(
    name: str,
    output: Path,
    context: Dict[str, Any] = {},
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    filename: Path = output / "index.html"

    template: Template = environ.get_template(name)
    rendered: str = await template.render_async(**context)
    async with aiofiles.open(filename, "wt", encoding="utf-8") as f:
        await f.write(rendered)


async def copy(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copytree, source, destination, dirs_exist_ok=True)


async def main() -> None:
    runtimes: List[str] = [b["runtime"] for b in database["bundles"]]
    bundles: List[Tuple[str, str]] = [(f"{b['organization']}/{b['repository']}", b["release"]) for b in database["bundles"]]

    semaphore: asyncio.Semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        coros = chain(
            (download_runtime(runtime, client, semaphore) for runtime in runtimes),
            (download_bundle(repo, rel, client, semaphore) for repo, rel in bundles),
        )

        await asyncio.gather(*[asyncio.create_task(coro) for coro in coros])

    await render("index.html", Path("output"), database)

    mapping = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}
    for b in database["bundles"]:
        width, height = mapping[b["resolution"]]
        context = {
            **b,
            "width": width,
            "height": height,
        }

        await render("play.html", Path("output") / "play" / b["slug"], context)

    await copy(Path("media"), Path("output") / "media")


if __name__ == "__main__":
    asyncio.run(main())
