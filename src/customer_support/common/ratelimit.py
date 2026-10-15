"""进程内令牌桶限流（按用户 / IP 维度）。

为什么客服系统必须限流：

- **LLM 调用是真金白银**：一次问答 = 一次意图识别 + 一次检索 + 一次生成。
  一个脚本化的调用方能在几分钟内烧掉整天的预算；
- **检索后端是共享资源**：RAGFlow 被刷会导致所有正常买家的响应一起变慢；
- **失败要可解释**：超限必须返回 429 + ``Retry-After``，让调用方按节奏退避，
  而不是死循环重试。

算法选**令牌桶**而不是固定窗口：桶允许少量突发（买家连问三句很正常），
但长期速率被 ``refill_per_second`` 卡死。桶容量 = 允许的突发量。

键的选择：已登录用户用 ``user:{id}``，匿名用 ``ip:{addr}``。同一用户即使换 IP
也限得住（防代理池），匿名请求则退化为按 IP 限（防脚本）。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict


class TokenBucket:
    """单个键的令牌桶。"""

    __slots__ = ("capacity", "refill_per_second", "tokens", "updated_at")

    def __init__(self, capacity: float, refill_per_second: float, now: float):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = capacity
        self.updated_at = now

    def take(self, now: float) -> tuple[bool, int]:
        """尝试取一个令牌，返回 ``(是否允许, 需等待秒数)``。"""
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0
        deficit = 1.0 - self.tokens
        wait = int(deficit / self.refill_per_second) + 1 if self.refill_per_second > 0 else 60
        return False, wait


class RateLimiter:
    """按键维度维护令牌桶（LRU 淘汰，防止键无限增长吃内存）。"""

    def __init__(
        self,
        *,
        capacity: float,
        per_minute: float,
        max_keys: int = 20000,
    ):
        self.capacity = max(1.0, capacity)
        self.refill_per_second = max(0.0, per_minute) / 60.0
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def check(self, key: str) -> tuple[bool, int]:
        """返回 ``(是否放行, 超限时建议等待秒数)``。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self.capacity, self.refill_per_second, now)
                self._buckets[key] = bucket
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            return bucket.take(now)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def matches(self, *, capacity: float, per_minute: float) -> bool:
        """当前限流参数是否与给定配置一致（用于运行期改配置时重建）。"""
        return (
            self.capacity == max(1.0, float(capacity))
            and self.refill_per_second == max(0.0, float(per_minute)) / 60.0
        )

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"tracked_keys": len(self._buckets)}


# 不同端点用不同的限流器（登录比聊天更敏感：防撞库）
_limiters: dict[str, RateLimiter] = {}
_registry_lock = threading.Lock()


def get_limiter(name: str, *, capacity: float, per_minute: float) -> RateLimiter:
    """按名字取限流器；名字相同但**参数变了**则重建。

    早期实现只在首次创建时应用参数，之后传入的 ``capacity`` / ``per_minute``
    被静默忽略——于是「改了 ``RATELIMIT_CHAT_PER_MINUTE`` 却完全没生效」这类
    问题极难排查（配置里写着 10，实际还按老值放行）。
    """
    with _registry_lock:
        limiter = _limiters.get(name)
        if limiter is None or not limiter.matches(capacity=capacity, per_minute=per_minute):
            limiter = RateLimiter(capacity=capacity, per_minute=per_minute)
            _limiters[name] = limiter
        return limiter


def reset_limiters() -> None:
    with _registry_lock:
        for limiter in _limiters.values():
            limiter.reset()
