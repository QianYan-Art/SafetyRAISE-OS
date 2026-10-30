from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)

# 报告执行与请求连接解耦：事件流断开不取消运行，任务在此持有强引用直至结束。
_tasks: set[asyncio.Task] = set()


def start(coroutine: Coroutine, *, run_id: str) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    _tasks.add(task)

    def finished(done: asyncio.Task) -> None:
        _tasks.discard(done)
        if not done.cancelled() and done.exception() is not None:
            logger.warning("后台报告执行异常结束。", extra={
                "run_id": run_id, "error_type": type(done.exception()).__name__,
            })

    task.add_done_callback(finished)
    return task


def active_count() -> int:
    return sum(1 for task in _tasks if not task.done())


async def shutdown(timeout: float = 10) -> None:
    """进程退出时取消仍在执行的报告；执行器自身会把运行持久记为已取消。"""
    pending = [task for task in _tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=timeout)
