"""熔断、降级与依赖健康监控。

为什么需要它——本项目真实踩过的坑：

DeepSeek 账户欠费后，API 返回 ``402 Insufficient Balance``。当时的链路是「每次都
真去调一次 LLM → 每次失败 → 每次都转人工」，用户看到的是"所有问题都转人工"，
而运维只有翻日志才知道是欠费。两个问题：

1. **没有快速失败**：依赖已经坏了，还每次去撞墙，白白消耗超时时间；
2. **故障不可见**：健康检查只验"key 有没有配"，不验"key 还能不能用"。

这里提供两件事：

- ``CircuitBreaker``：连续失败 N 次后**打开**，后续调用直接快速失败（不再撞墙）；
  冷却 ``recovery_timeout`` 秒后进入半开态，放一个探针请求试探，成功则闭合、失败则继续打开；
- ``LLMHealthMonitor``：记录最近一次成功/失败、错误分类（欠费/鉴权/限流/超时/网络），
  把「欠费」这种必须运维介入的故障显式暴露到 ``/readyz`` 与 ``/metrics``。

两者都是**进程内**状态。单机部署形态下这足够了；多副本时每个副本各自熔断，
如果将来要多副本共享熔断状态，把状态搬进 Redis 即可（接口不变）。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Any, TypeVar

from customer_support.common.exceptions import ErrorCode, ServiceUnavailableError
from customer_support.common.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"        # 正常放行
    OPEN = "open"            # 已熔断，快速失败
    HALF_OPEN = "half_open"  # 试探中，只放一个请求


class CircuitOpenError(ServiceUnavailableError):
    """熔断打开时抛出：调用方应快速降级，而不是继续等待。"""

    code = ErrorCode.SERVICE_UNAVAILABLE

    def __init__(self, name: str, retry_after_seconds: int = 0):
        super().__init__(
            "服务暂时不可用，请稍后再试",
            detail=f"circuit '{name}' is open",
        )
        self.retry_after_seconds = retry_after_seconds


class CircuitBreaker:
    """轻量熔断器（线程安全）。"""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
    ):
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        # 下限只挡负数：亚秒级冷却是合法配置（快速恢复依赖 / 单测），不应被强行抬到 1s
        self.recovery_timeout = max(0.0, float(recovery_timeout))
        self.half_open_max_calls = max(1, half_open_max_calls)

        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at = 0.0
        self._half_open_calls = 0

    # ---------------- 状态查询 ----------------
    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        """冷却时间到 → 自动从 OPEN 切到 HALF_OPEN（调用方无需关心时间）。"""
        if self._state is CircuitState.OPEN and time.monotonic() - self._opened_at >= self.recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._maybe_half_open()
            retry_after = 0
            if self._state is CircuitState.OPEN:
                elapsed = time.monotonic() - self._opened_at
                retry_after = max(0, int(self.recovery_timeout - elapsed))
            return {
                "name": self.name,
                "state": self._state.value,
                "failures": self._failures,
                "successes": self._successes,
                "retry_after_seconds": retry_after,
            }

    # ---------------- 调用与记账 ----------------
    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """执行受保护调用：熔断打开时直接抛 ``CircuitOpenError``。"""
        self.acquire()
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def acquire(self) -> None:
        """进入受保护区：熔断打开时抛 ``CircuitOpenError``（供 ``with`` 用法配合）。"""
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN:
                raise CircuitOpenError(self.name, self.snapshot()["retry_after_seconds"])
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    raise CircuitOpenError(self.name, int(self.recovery_timeout))
                self._half_open_calls += 1

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._successes += 1
            self._state = CircuitState.CLOSED
            self._half_open_calls = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                if self._state is not CircuitState.OPEN:
                    logger.warning(
                        "熔断器 %s 打开（连续失败 %d 次，冷却 %.1fs）",
                        self.name, self._failures, self.recovery_timeout,
                    )
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._half_open_calls = 0


# ----------------------------------------------------------------------
# LLM 错误分类与健康监控
# ----------------------------------------------------------------------
_ERROR_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("quota", ("insufficient balance", "insufficient_quota", "quota", "欠费", "余额不足")),
    ("auth", ("invalid api key", "incorrect api key", "401", "unauthorized", "authentication")),
    ("rate_limit", ("rate limit", "429", "too many requests")),
    # 上游过载 / 网关故障：既不是我们的鉴权问题，也不是网络不通，
    # 而是对方临时扛不住。实测 DeepSeek 高峰期会直接返回
    # 「503 Service is too busy」——原先没有这一类，会被归成 unknown，
    # /readyz 的 last_error_category 显示 unknown，运维看不出到底怎么了。
    (
        "unavailable",
        (
            "503", "502", "504", "service is too busy", "service_unavailable",
            "overloaded", "bad gateway", "gateway timeout",
        ),
    ),
    ("timeout", ("timeout", "timed out", "read timeout")),
    ("connection", ("connection", "connect", "dns", "refused", "network")),
]


def is_dependency_unavailable(exc: BaseException) -> bool:
    """异常是否表示「外部依赖暂时不可用」，而非「我们自己的代码/输入有问题」。

    为什么需要这个区分——启动项目时实测踩到的坑：

    意图识别的 LLM 调用超时 / 503 时，Agent 层原先一律兜底成 ``intent=human``，
    ChatService 再按「命中高风险意图」归因，工单上写的是 ``high_risk_intent``。
    结果明明是 DeepSeek 在抖，运营看到的却是「买家投诉激增」，
    而专门为依赖故障定义的 ``service_unavailable`` 永远不出现。

    判定顺序（都不依赖具体 SDK 版本，避免 openai SDK 升级就失配）：

    1. 熔断已打开——依赖明确不可用；
    2. HTTP 状态码 5xx / 408 / 429（有的 SDK 挂在 ``exc.status_code``，
       有的挂在 ``exc.response.status_code``）；
    3. 错误分类能识别出上游故障类别（超时/连接/限流/欠费/鉴权/过载）。

    只有 ``unknown`` 才算「可能是我们自己的问题」，归 ``system_error``。
    """
    if isinstance(exc, CircuitOpenError):
        return True

    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and (status >= 500 or status in (408, 429)):
        return True

    return classify_llm_error(exc) != "unknown"


def classify_llm_error(exc: BaseException) -> str:
    """把 LLM 异常归类，便于统计与给出对症的运维提示。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    for category, needles in _ERROR_PATTERNS:
        if any(n in text for n in needles):
            return category
    return "unknown"


# 健康信息「过期」窗口：失败记录超过这个时长且期间没有任何新调用时，
# 视为「信息已陈旧、无法判定」，而不是继续断言依赖不可用。
#
# 为什么必须有这个窗口：``ok`` 原先等价于「连续失败次数 == 0」，而失败计数只能被
# **下一次成功调用**清零。若一次偶发超时之后再没有流量（夜间、低峰），``/readyz``
# 会永久返回 503——K8s 会把这个其实健康的副本一直摘出负载均衡，直到有人手动发一次
# 问答去"治好"它。这与本模块文档里「最近 5 分钟内有成功即视为健康」的说法也不一致。
HEALTH_STALE_SECONDS = 300.0


class LLMHealthMonitor:
    """记录 LLM 可用性，供 /readyz 与 /metrics 暴露（欠费等故障一眼可见）。"""

    def __init__(self, stale_after_seconds: float = HEALTH_STALE_SECONDS) -> None:
        self._lock = threading.Lock()
        self.stale_after_seconds = max(0.0, float(stale_after_seconds))
        self._last_success_at: float | None = None
        self._last_failure_at: float | None = None
        self._last_error: str = ""
        self._last_category: str = ""
        self._consecutive_failures = 0
        self._total_calls = 0
        self._total_failures = 0

    def record_success(self) -> None:
        with self._lock:
            self._last_success_at = time.time()
            self._consecutive_failures = 0
            self._total_calls += 1

    def record_failure(self, exc: BaseException) -> str:
        category = classify_llm_error(exc)
        with self._lock:
            self._last_failure_at = time.time()
            self._last_error = f"{type(exc).__name__}: {exc}"[:400]
            self._last_category = category
            self._consecutive_failures += 1
            self._total_calls += 1
            self._total_failures += 1
        if category == "quota":
            logger.error(
                "LLM 账户余额/配额不足（%s）——所有问答将降级转人工，请尽快充值或换 key",
                exc,
            )
        return category

    def _is_stale_locked(self) -> bool:
        """失败信息是否已陈旧（调用方需持有 ``self._lock``）。"""
        return (
            self._consecutive_failures > 0
            and self._last_failure_at is not None
            and (time.time() - self._last_failure_at) > self.stale_after_seconds
        )

    @property
    def quota_exhausted(self) -> bool:
        with self._lock:
            # 陈旧的欠费结论同样不再上报：否则三天前的一次欠费会让指标一直亮红灯
            return (
                self._last_category == "quota"
                and self._consecutive_failures > 0
                and not self._is_stale_locked()
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            # 判定分三种情况：
            #   ① 从未失败过 → 健康；
            #   ② 有连续失败、且最近一次失败还在窗口内 → 不健康（真的坏了）；
            #   ③ 有连续失败、但最近一次失败已超出窗口（此后没有任何调用）→ 信息陈旧，
            #      不再断言"不可用"，只把 stale 标出来让运维知道"这是没流量的旧结论"。
            stale = self._is_stale_locked()
            healthy = self._consecutive_failures == 0 or stale
            return {
                "ok": healthy,
                "stale": stale,
                "quota_exhausted": self._last_category == "quota"
                and self._consecutive_failures > 0
                and not stale,
                "last_error_category": self._last_category or None,
                "last_error": self._last_error or None,
                "consecutive_failures": self._consecutive_failures,
                "total_calls": self._total_calls,
                "total_failures": self._total_failures,
                "last_success_ago_seconds": (
                    round(now - self._last_success_at) if self._last_success_at else None
                ),
                "last_failure_ago_seconds": (
                    round(now - self._last_failure_at) if self._last_failure_at else None
                ),
            }


# ----------------------------------------------------------------------
# 进程级注册表
# ----------------------------------------------------------------------
_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()
_llm_health = LLMHealthMonitor()


def get_breaker(name: str, **kwargs: Any) -> CircuitBreaker:
    """按名字取（或创建）熔断器，保证同一依赖全进程共用一个。

    ⚠️ ``kwargs`` **只在首次创建时生效**：若同名熔断器已存在，这里不会覆盖它的阈值。
    运行期改阈值请用 ``configure_breaker``（它才是幂等的配置入口）——这个区分能避免
    「某个调用点悄悄改掉全局阈值」这类难查的 bug。
    """
    with _registry_lock:
        breaker = _breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(name, **kwargs)
            _breakers[name] = breaker
        return breaker


def get_llm_health() -> LLMHealthMonitor:
    return _llm_health


def configure_breaker(name: str, **kwargs: Any) -> CircuitBreaker:
    """按配置更新已存在熔断器的阈值（启动时由 deps 调用）。"""
    breaker = get_breaker(name)
    with breaker._lock:
        if "failure_threshold" in kwargs:
            breaker.failure_threshold = max(1, int(kwargs["failure_threshold"]))
        if "recovery_timeout" in kwargs:
            breaker.recovery_timeout = max(0.0, float(kwargs["recovery_timeout"]))
    return breaker


@contextmanager
def guarded(name: str, *, health: bool = False) -> Iterator[None]:
    """受保护代码块：``with guarded("llm", health=True): ...``。

    成功/异常自动记账；``health=True`` 时同时更新 LLM 健康监控（用于 /readyz 暴露欠费等故障）。
    熔断打开时进入即抛 ``CircuitOpenError``——调用方应快速降级而不是继续等待。
    """
    breaker = get_breaker(name)
    monitor = get_llm_health() if health else None
    breaker.acquire()
    try:
        yield
    except Exception as exc:
        breaker.record_failure()
        if monitor is not None:
            monitor.record_failure(exc)
        raise
    else:
        breaker.record_success()
        if monitor is not None:
            monitor.record_success()


def all_breaker_snapshots() -> list[dict[str, Any]]:
    with _registry_lock:
        return [b.snapshot() for b in _breakers.values()]


def reset_resilience() -> None:
    """把熔断与健康状态恢复到「进程刚启动」的样子（测试用）。

    注意：这里**清空注册表**而不是逐个 ``reset()``。因为 ``configure_breaker`` 会改写
    熔断器的阈值并常驻，若只重置状态而不丢弃实例，某个测试临时设的
    ``failure_threshold=1`` 会泄漏给后续所有测试（表现为「单独跑全绿、全量跑崩一片」）。
    """
    with _registry_lock:
        _breakers.clear()
    global _llm_health
    _llm_health = LLMHealthMonitor()
