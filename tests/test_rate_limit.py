import asyncio

from app.core.rate_limit import UserRateLimiter


def test_rate_limiter_blocks_rapid_requests() -> None:
    async def run() -> None:
        limiter = UserRateLimiter(min_interval=10)
        assert await limiter.allow(123)
        assert not await limiter.allow(123)
        assert await limiter.allow(456)

    asyncio.run(run())
