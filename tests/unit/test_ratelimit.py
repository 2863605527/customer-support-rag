"""令牌桶限流单测。"""
from customer_support.common.ratelimit import RateLimiter, TokenBucket


class TestTokenBucket:
    def test_allows_burst_then_blocks(self):
        bucket = TokenBucket(capacity=3, refill_per_second=0.0, now=0.0)
        assert bucket.take(0.0)[0] is True
        assert bucket.take(0.0)[0] is True
        assert bucket.take(0.0)[0] is True
        allowed, wait = bucket.take(0.0)
        assert allowed is False
        assert wait >= 1

    def test_refills_over_time(self):
        bucket = TokenBucket(capacity=2, refill_per_second=1.0, now=0.0)
        bucket.take(0.0)
        bucket.take(0.0)
        assert bucket.take(0.0)[0] is False
        assert bucket.take(1.0)[0] is True  # 1 秒补 1 个令牌


class TestRateLimiter:
    def test_per_key_isolation(self):
        limiter = RateLimiter(capacity=1, per_minute=60)
        assert limiter.check("user:a")[0] is True
        assert limiter.check("user:a")[0] is False
        assert limiter.check("user:b")[0] is True  # 另一个用户不受影响

    def test_reset_clears_state(self):
        limiter = RateLimiter(capacity=1, per_minute=60)
        limiter.check("k")
        assert limiter.check("k")[0] is False
        limiter.reset("k")
        assert limiter.check("k")[0] is True

    def test_lru_evicts_oldest_keys(self):
        limiter = RateLimiter(capacity=1, per_minute=60, max_keys=2)
        limiter.check("a")
        limiter.check("b")
        limiter.check("c")
        assert limiter.stats()["tracked_keys"] == 2
