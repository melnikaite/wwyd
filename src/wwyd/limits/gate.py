# SPDX-License-Identifier: AGPL-3.0-or-later
"""Global LLM concurrency gate: at most N calls in flight, bounded wait, bounded call time."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import TypeVar

from wwyd.errors import LLMUnavailable

T = TypeVar("T")


class LLMGate:
    def __init__(self, concurrency: int, queue_timeout_s: float, call_timeout_s: float) -> None:
        self.concurrency = max(1, concurrency)
        self.queue_timeout_s = queue_timeout_s
        self.call_timeout_s = call_timeout_s
        self._sem = asyncio.Semaphore(self.concurrency)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Wait up to the queue timeout for a slot, else raise :class:`LLMUnavailable`."""
        try:
            await asyncio.wait_for(self._sem.acquire(), self.queue_timeout_s)
        except TimeoutError:
            raise LLMUnavailable("LLM busy, try again later", retry_after=5) from None
        try:
            yield
        finally:
            self._sem.release()

    async def run(self, call: Awaitable[T]) -> T:
        """Run ``call`` inside a slot with the call timeout."""
        async with self.slot():
            try:
                return await asyncio.wait_for(call, self.call_timeout_s)
            except TimeoutError:
                raise LLMUnavailable("LLM call timed out") from None
