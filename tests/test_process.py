from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from xcraft import process


def should_cancel_pending_tasks_when_interrupted() -> None:
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


def should_wait_for_all_tasks_even_on_error() -> None:
    ran = []
    with process.thread_pool(2) as pool:
        for i in range(4):
            pool.submit(ran.append, i)
    assert sorted(ran) == [0, 1, 2, 3]
    # on an error too, all of them: what follows, like a cleanup, may need them done
    ran.clear()
    with pytest.raises(ValueError, match="failed"), process.thread_pool(2) as pool:  # noqa: PT012
        for i in range(4):
            pool.submit(lambda i: (time.sleep(0.1), ran.append(i)), i)
        raise ValueError("failed")  # noqa: EM101
    assert sorted(ran) == [0, 1, 2, 3]
