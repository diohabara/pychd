"""Async 'promise' wrapper with cancel + timeout-or-default behaviour."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class Promise(Generic[T]):
    def __init__(self, factory: Callable[[], Awaitable[T]]):
        self._factory = factory
        self._task: asyncio.Task[T] | None = None
        self._result: T | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> T:
        try:
            value = await self._factory()
        except BaseException as exc:
            self._error = exc
            raise
        self._result = value
        return value

    async def await_or_default(self, default: T, timeout: float) -> T:
        self.start()
        assert self._task is not None
        try:
            return await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except asyncio.TimeoutError:
            return default

    def cancel(self) -> bool:
        if self._task is None or self._task.done():
            return False
        return self._task.cancel()


@asynccontextmanager
async def background(factory: Callable[[], Awaitable[T]]) -> AsyncIterator[Promise[T]]:
    promise = Promise(factory)
    promise.start()
    try:
        yield promise
    finally:
        promise.cancel()


async def zipped_first(*factories: Callable[[], Awaitable[T]]) -> list[T]:
    promises = [Promise(f) for f in factories]
    for p in promises:
        p.start()
    out: list[T] = []
    for p in promises:
        out.append(await p.await_or_default(default=None, timeout=5.0))  # type: ignore[arg-type]
    return out
