import asyncio
from unittest.mock import patch

from chat2api.anti_detection.rate_limiter import AccountLock, RateLimiter


async def _exercise_account_lock() -> None:
    lock = AccountLock()
    assert await lock.try_acquire("user1") is True
    assert await lock.try_acquire("user1") is False
    assert await lock.try_acquire("user2") is True

    lock.release("user1")
    assert await lock.try_acquire("user1") is True


def test_account_lock():
    asyncio.run(_exercise_account_lock())


def test_rate_limiter():
    limiter = RateLimiter(max_rpm=2)
    with patch("time.time", return_value=100.0):
        assert limiter.check("user1") is True
        assert limiter.check("user1") is True
        assert limiter.check("user1") is False
        assert limiter.check("user2") is True
    
    # Fast forward 60 seconds
    with patch("time.time", return_value=160.0):
        # The first two requests expire
        assert limiter.check("user1") is True
        assert limiter.check("user1") is True
        assert limiter.check("user1") is False
