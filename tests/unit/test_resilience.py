"""熔断器与 LLM 健康监控单测。

固化的行为：连续失败会「打开」熔断并快速失败；冷却后进入半开试探；
欠费类错误被识别为 ``quota``（这正是线上真实踩过的坑）。
"""
import pytest

from customer_support.common.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    LLMHealthMonitor,
    classify_llm_error,
    guarded,
    reset_resilience,
)


def _boom():
    raise RuntimeError("upstream down")


class TestCircuitBreaker:
    def test_opens_after_threshold_and_fails_fast(self):
        breaker = CircuitBreaker("t1", failure_threshold=3, recovery_timeout=60)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                breaker.call(_boom)
        assert breaker.state.value == "open"

        # 打开后不再调用上游：直接抛 CircuitOpenError（快速失败）
        called = {"n": 0}

        def probe():
            called["n"] += 1
            return "ok"

        with pytest.raises(CircuitOpenError):
            breaker.call(probe)
        assert called["n"] == 0

    def test_half_open_recovers_after_cooldown(self):
        breaker = CircuitBreaker("t2", failure_threshold=1, recovery_timeout=0.01)
        with pytest.raises(RuntimeError):
            breaker.call(_boom)
        assert breaker.state.value == "open"

        import time

        time.sleep(0.02)
        assert breaker.call(lambda: "recovered") == "recovered"
        assert breaker.state.value == "closed"

    def test_success_resets_failure_count(self):
        breaker = CircuitBreaker("t3", failure_threshold=3)
        with pytest.raises(RuntimeError):
            breaker.call(_boom)
        breaker.record_success()
        assert breaker.snapshot()["failures"] == 0

    def test_guarded_context_records_failure(self):
        reset_resilience()
        with pytest.raises(RuntimeError):
            with guarded("unit-guard", health=True):
                raise RuntimeError("boom")
        from customer_support.common.resilience import get_llm_health

        assert get_llm_health().snapshot()["consecutive_failures"] == 1


class TestErrorClassification:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("Error code: 402 - Insufficient Balance", "quota"),
            ("Incorrect API key provided", "auth"),
            ("Rate limit reached for requests", "rate_limit"),
            ("Request timed out", "timeout"),
            ("Connection error", "connection"),
            ("something weird", "unknown"),
        ],
    )
    def test_classify(self, message, expected):
        assert classify_llm_error(RuntimeError(message)) == expected

    def test_quota_exhausted_flag(self):
        monitor = LLMHealthMonitor()
        monitor.record_failure(RuntimeError("402 Insufficient Balance"))
        snapshot = monitor.snapshot()
        assert snapshot["quota_exhausted"] is True
        assert snapshot["ok"] is False
        assert snapshot["last_error_category"] == "quota"

        monitor.record_success()
        assert monitor.snapshot()["quota_exhausted"] is False
