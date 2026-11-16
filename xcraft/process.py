"""Running commands and downloading files, logged at debug level, to follow what xcraft does: xcraft --debug shows them.

The operations on the remote storage are logged there too, in xcraft.remote.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

log = logging.getLogger("xcraft")


@contextmanager
def thread_pool(workers: int) -> Generator[ThreadPoolExecutor]:
    """Return a pool of `workers` threads, which waits for its tasks at the end, but on Ctrl-C: the tasks not started
    are cancelled, and those running left to the end of the process, which xcraft's main() does not wait for.
    """
    pool = ThreadPoolExecutor(workers)
    try:
        yield pool
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    input: str | None = None,  # noqa: A002, like subprocess.run
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing its output with `capture`, and return how it went; with `check`, fail when it fails."""
    log.debug("running %s", shlex.join(cmd))
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, input=input, env=env)


def run_bytes(cmd: list[str], input: bytes = b"") -> bytes:  # noqa: A002, like subprocess.run
    """Run `cmd`, failing when it fails, and return its output, as is: for streams like those of git fast-export."""
    log.debug("running %s", shlex.join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, input=input).stdout


def popen(
    cmd: list[str],
    *,
    stdin: int | None = None,
    stdout: int | None = None,
    stderr: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    """Start `cmd`, with the given standard streams, for its output as it comes, or to write to it."""
    log.debug("starting %s", shlex.join(cmd))
    return subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr, text=True, errors="replace", env=env)


def download(url: str, dest: Path, report: Callable[[int, int, int], None] | None = None) -> None:
    """Download `url` to `dest`, giving the progress to `report`, like urlretrieve, and raise OSError when it fails,
    like when the server closes the connection too soon.
    """
    import http.client  # noqa: PLC0415, imported once needed, not at each start
    import urllib.request  # noqa: PLC0415

    log.debug("downloading %s to %s", url, dest)
    try:
        urllib.request.urlretrieve(url, dest, reporthook=report)  # noqa: S310
    except http.client.HTTPException as e:
        msg = f"{url}: {e!r}"
        raise OSError(msg) from e


def git(*args: str) -> str:
    """Run git and return its stripped output."""
    return run(["git", *args], capture=True).stdout.strip()
