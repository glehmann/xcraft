from __future__ import annotations

import threading
from concurrent.futures import Future

import pytest

from xcraft import process


def test_thread_pool_interrupted() -> None:
    started, release, ran = threading.Event(), threading.Event(), []

    def task(i: int) -> None:
        started.set()
        release.wait(5)
        ran.append(i)

    futures: list[Future[None]] = []

    def interrupted() -> None:
        with process.thread_pool(1) as pool:
            futures.extend(pool.submit(task, i) for i in range(3))
            started.wait(5)
            raise KeyboardInterrupt  # Ctrl-C, while the first task runs

    with pytest.raises(KeyboardInterrupt):
        interrupted()
    # the others never start, and the first one is not waited for
    assert [f.cancelled() for f in futures] == [False, True, True]
    assert not futures[0].done()
    release.set()
    futures[0].result(5)
    assert ran == [0]


def test_thread_pool_waits() -> None:
    ran = []
    with process.thread_pool(2) as pool:
        for i in range(4):
            pool.submit(ran.append, i)
    assert sorted(ran) == [0, 1, 2, 3]
