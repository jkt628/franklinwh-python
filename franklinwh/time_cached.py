"""Cache a function result for a specified time.

This design provides a locked cache PER DECORATOR INSTANCE so SHOULD apply
only to functions that are definitely called periodically, otherwise it may
cache arguments and results indefinitely, think 'self' for member functions.
"""

import asyncio
from datetime import timedelta
from functools import wraps

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
