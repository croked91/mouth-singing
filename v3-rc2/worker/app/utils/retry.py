"""Generic async retry helper with exponential backoff."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def retry_async(
    coro_factory: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay_sec: float = 1.0,
    retryable_exceptions: tuple = (Exception,),
    non_retryable_exceptions: tuple = (),
) -> T:
    """Call coro_factory up to max_retries times with exponential backoff.

    A fresh coroutine is created on each attempt by calling coro_factory.
    If the raised exception matches non_retryable_exceptions it is re-raised
    immediately without further attempts. After all retries are exhausted the
    last exception is re-raised.

    Args:
        coro_factory: Zero-argument callable that returns a new awaitable each
            time it is called.
        max_retries: Maximum number of attempts (including the first call).
        base_delay_sec: Delay before the second attempt. Each subsequent delay
            is doubled: base * 2^attempt.
        retryable_exceptions: Exception types that trigger a retry.
        non_retryable_exceptions: Exception types that are re-raised without
            retrying, checked before retryable_exceptions.

    Returns:
        The return value of the successful coroutine call.

    Raises:
        Exception: The last exception raised after all retries are exhausted,
            or the first non-retryable exception encountered.
    """
    last_exception: BaseException | None = None

    for attempt in range(max_retries):
        try:
            return await coro_factory()
        except non_retryable_exceptions:
            raise
        except retryable_exceptions as exc:
            last_exception = exc
            if attempt < max_retries - 1:
                delay = base_delay_sec * (2 ** attempt)
                await asyncio.sleep(delay)

    raise last_exception
