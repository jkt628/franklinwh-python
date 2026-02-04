"""Cache a function result for a specified time.

This design provides a locked cache PER DECORATOR INSTANCE so SHOULD apply
only to functions that are definitely called periodically, otherwise it may
cache arguments and results indefinitely, think 'self' for member functions.
"""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from typing import Any

from cachetools import TTLCache


def time_cached(ttl: timedelta = timedelta(seconds=2)):
    """Decorator to cache function results for a specified time-to-live (TTL)."""

    def wrapper(func):
        __cache = TTLCache(maxsize=10, ttl=ttl.seconds)
        __lock = asyncio.Lock()

        @wraps(func)
        async def wrapped(*args, **kwargs):
            async with __lock:
                key = (args, frozenset(kwargs.items()))
                if key in __cache:
                    return __cache[key]
                value = await func(*args, **kwargs)
                __cache[key] = value
                return value

        wrapped.clear = __cache.clear
        return wrapped

    return wrapper


class also_clear:
    """Decorator to clear wrapped and additional objects."""

    @staticmethod
    def clearable(extra: Callable[..., Any]) -> None:
        """Ensure clear()."""
        if not callable(getattr(extra, "clear", None)):
            raise TypeError(f"{extra} missing clear()")

    def __init__(self, extra: Callable[..., Any] | list[Callable[..., Any]]) -> None:
        """Collect additional objects to clear()."""
        if callable(extra):
            extra = [extra]
        if not extra:
            raise ValueError("extra must not be empty")
        for func in extra:
            self.clearable(func)
        self.extra: list[Callable[..., Any]] = extra

    def clear(self):
        """Clear wrapped and additional objects."""
        for extra in self.extra:
            extra.clear()

    def __call__(self, func):
        """Wrap function."""

        @wraps(func)
        async def wrapped(*args, **kwargs):
            return await func(*args, **kwargs)

        self.clearable(func)
        self.extra.append(func)
        wrapped.clear = self.clear
        return wrapped
