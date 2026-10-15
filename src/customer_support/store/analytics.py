"""运营分析：把散落在各张表里的数据聚合成「一眼能看懂」的运营视图。

两个出口：

- ``overview()``：给仪表盘 / API 用，返回结构化 JSON（含趋势、分布、好评率）；
- ``metrics_text()``：给 Prometheus / 监控系统用，输出文本格式指标。

指标选择的原则是**能驱动决策**，不是越多越好：

| 指标 | 回答什么问题 |
|---|---|
| 自助解决率 containment_rate | AI 到底顶了多少事？（越高越好） |
| 转人工率 + 转人工原因分布 | 转出去的都是什么原因？该优化检索还是该改阈值？ |
| 平均延迟（排除缓存命中） | 真实服务端耗时是多少？（缓存不能算进来"美化"） |
| 好评率 + 差评原因分布 | 答得准不准？最该先修哪一类问题？ |
| 工单积压 / 超时 / 首次响应时长 | 人工侧扛得住吗？有没有漏处理的单？ |
| LLM 健康（欠费/熔断） | 现在到底是"没资料"还是"模型挂了"？ |
"""
from __future__ import annotations

from typing import Any

from customer_support.common.resilience import LLMHealthMonitor, all_breaker_snapshots
from customer_support.store.cache import AnswerCacheStore
from customer_support.store.conversations import ConversationStore
from customer_support.store.feedback import FeedbackStore
from customer_support.store.tickets import TicketStore


class AnalyticsService:
    """运营指标聚合器。"""

    def __init__(
        self,
        conversations: ConversationStore,
        tickets: TicketStore,
        feedback: FeedbackStore,
        cache: AnswerCacheStore,
        llm_health: LLMHealthMonitor,
    ):
        self.conversations = conversations
        self.tickets = tickets
        self.feedback = feedback
        self.cache = cache
        self.llm_health = llm_health

    def overview(self, *, days: int = 7) -> dict[str, Any]:
        """运营总览（仪表盘主接口）。"""
        conv = self.conversations.metrics(days=days)
        fb = self.feedback.stats(days=days)
        return {
            "window_days": days,
            "conversation": conv,
            "ticket": self.tickets.stats(days=days),
            "feedback": fb.model_dump(mode="json"),
            "cache": self.cache.stats(),
            "llm": self.llm_health.snapshot(),
            "breakers": all_breaker_snapshots(),
        }

    def metrics_text(self, *, days: int = 7) -> str:
        """Prometheus 文本格式（聚合值，不含任何买家隐私数据）。"""
        conv = self.conversations.metrics(days=days)
        fb = self.feedback.stats(days=days)
        ticket = self.tickets.stats(days=days)
        cache = self.cache.stats()
        llm = self.llm_health.snapshot()

        lines: list[str] = []

        def gauge(name: str, value: float | int | None, help_text: str) -> None:
            if value is None:
                return
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")

        def label(value: object) -> str:
            """转义标签值——未转义的 ``"`` / 换行会让整个文本格式非法，
            导致 Prometheus 抓取直接失败（而不是丢掉这一行）。"""
            return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

        gauge("cs_conversations", conv["conversations"], "会话数（窗口内）")
        gauge("cs_questions_total", conv["questions"], "买家提问数（窗口内）")
        gauge("cs_answers_total", conv["answers"], "AI 回答数（窗口内）")
        gauge("cs_handoffs_total", conv["handoffs"], "转人工数（窗口内）")
        gauge("cs_containment_rate", conv["containment_rate"], "自助解决率（未转人工占比）")
        gauge("cs_avg_latency_ms", conv["avg_latency_ms"], "平均响应耗时（毫秒，不含缓存命中）")
        gauge("cs_feedback_positive_rate", fb.positive_rate, "回答好评率")
        gauge("cs_feedback_down_total", fb.down, "差评数")
        gauge("cs_tickets_backlog", ticket["backlog"], "未处理工单数")
        gauge("cs_tickets_overdue", ticket["overdue"], "超时未处理工单数")
        gauge("cs_cache_entries", cache["entries"], "问答缓存条目数")
        gauge("cs_cache_hits", cache["hits"], "问答缓存累计命中数")
        gauge("cs_llm_up", 1 if llm["ok"] else 0, "LLM 最近一次调用是否健康")
        gauge("cs_llm_quota_exhausted", 1 if llm["quota_exhausted"] else 0, "LLM 是否处于欠费状态")

        for reason, count in sorted(conv["by_handoff_reason"].items()):
            lines.append(f'cs_handoff_by_reason{{reason="{label(reason)}"}} {count}')
        for source, count in sorted(conv["by_source"].items()):
            lines.append(f'cs_answers_by_source{{source="{label(source)}"}} {count}')
        for breaker in all_breaker_snapshots():
            lines.append(
                f'cs_circuit_state{{name="{label(breaker["name"])}"}} '
                f'{0 if breaker["state"] == "closed" else 1}'
            )

        return "\n".join(lines) + "\n"
