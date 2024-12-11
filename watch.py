import multiprocessing
import os
import signal
import subprocess
import time

from rx import create
from rx.core.typing import Observer
from rx.core.typing import Scheduler
from rx.disposable.disposable import Disposable
from rx.operators import debounce
from watchgod import watch

running: bool = True


def signal_handler(signum: int, frame: object) -> None:
    global running
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def build() -> None:
    try:
        subprocess.run(
            [
                "cmake",
                "--build",
                ".",
                "--parallel",
                str(multiprocessing.cpu_count()),
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr is not None:
            print(exc.stderr)


def main() -> None:
    os.chdir("/opt/carimbo")

    try:
        subprocess.run(
            [
                "conan",
                "install",
                ".",
                "--output-folder=build",
                "--build=missing",
                "--profile=webassembly",
                "--settings",
                "compiler.cppstd=20",
                "--settings",
                "build_type=Release",
            ],
            check=True,
        )

        os.chdir("build")

        subprocess.run(
            [
                "cmake",
                "..",
                "-DCMAKE_TOOLCHAIN_FILE=conan_toolchain.cmake",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DSANDBOX=OFF",
            ],
            check=True,
        )

        build()
    except subprocess.CalledProcessError as exc:
        if exc.stderr is not None:
            print(exc.stderr)
        raise RuntimeError(f"Failure in the build process with exit code {exc.returncode}") from exc

    def on_subscribe(observer: Observer, scheduler: Scheduler | None = None) -> Disposable:
        for _ in watch("/opt/carimbo"):
            if not running:
                break
            observer.on_next(0)
        return Disposable(lambda: None)

    subscription = create(on_subscribe).pipe(debounce(1)).subscribe(lambda _: build())

    while running:
        time.sleep(1)

    subscription.dispose()


if __name__ == "__main__":
    main()
