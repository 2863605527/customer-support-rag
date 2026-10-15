"""转人工工单模型：工单本体 + 人工回复 + 状态流转。

工单是「AI 转人工」的落地载体，因此它必须在 AI 之外**能被真人接手并走完**：
``open``（待处理）→ ``in_progress``（处理中）→ ``resolved``（已解决）→ ``closed``（已归档）。
状态、优先级、指派人、人工回复缺一不可——只记录不处理的「工单」等于没有工单。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

TicketStatus = Literal["open", "in_progress", "resolved", "closed"]
TicketPriority = Literal["low", "normal", "high", "urgent"]

# 转人工原因：与 ChatService 的判定规则一一对应（统计口径依赖这些字面量）
TransferReason = Literal[
    "high_risk_intent",  # 高风险诉求（退款/投诉/纠纷），不能自动承诺
    "low_confidence",    # 意图置信度低，宁可转人工也不猜
    "no_knowledge",      # 知识库没有足够资料，不编造
    "rate_limited",      # 触发限流
    "service_unavailable",  # LLM/检索熔断或欠费，服务暂不可用
    "system_error",      # 未预期异常降级
]

REASON_LABELS: dict[str, str] = {
    "high_risk_intent": "高风险诉求",
    "low_confidence": "置信度不足",
    "no_knowledge": "知识库无资料",
    "rate_limited": "触发限流",
    "service_unavailable": "服务暂不可用",
    "system_error": "系统异常",
}

STATUS_LABELS: dict[str, str] = {
    "open": "待处理",
    "in_progress": "处理中",
    "resolved": "已解决",
    "closed": "已归档",
}

PRIORITY_LABELS: dict[str, str] = {
    "low": "低",
    "normal": "中",
    "high": "高",
    "urgent": "紧急",
}

# 不同转人工原因的默认优先级：高风险与系统故障更急，避免被淹没在队列里
DEFAULT_PRIORITY: dict[str, str] = {
    "high_risk_intent": "high",
    "service_unavailable": "urgent",
    "system_error": "high",
    "low_confidence": "normal",
    "no_knowledge": "normal",
    "rate_limited": "low",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TicketReply(BaseModel):
    """工单上的一条人工回复 / 内部备注。"""

    id: int = Field(0, description="自增主键（0 表示尚未落库）")
    ticket_id: str = Field("")
    author: str = Field("", description="回复人（客服邮箱或显示名）")
    body: str = Field(..., min_length=1, description="回复正文")
    internal: bool = Field(False, description="True=内部备注（不发送给买家）")
    created_at: datetime = Field(default_factory=_utcnow)


class Ticket(BaseModel):
    """转人工工单：高风险 / 低置信度 / 资料不足时自动创建，供人工客服接手。"""

    ticket_id: str = Field(..., description="工单号")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    status: TicketStatus = Field("open", description="open / in_progress / resolved / closed")
    priority: TicketPriority = Field("normal", description="low / normal / high / urgent")
    assignee: str = Field("", description="处理人（客服邮箱；空=未指派）")

    question: str = Field(..., description="买家原问题")
    language: str = Field("unknown")
    intent: str = Field("unknown")
    confidence: float = Field(0.0, ge=0, le=1)
    reason: str = Field(
        "",
        description=(
            "high_risk_intent（高风险）/ low_confidence（低置信度）/ "
            "no_knowledge（资料不足）/ rate_limited（限流）/ "
            "service_unavailable（服务不可用）/ system_error（系统异常降级）"
        ),
    )
    asked_answer: str = Field("", description="系统当时给出的回复（含兜底话术）")

    buyer_id: str | None = Field(None)
    session_id: str | None = Field(None)
    trace_id: str = Field("")
    sources: list[str] = Field(default_factory=list, description="命中的知识库来源（便于人工快速定位）")

    # SLA 时间戳：首次响应 / 解决时间，用于计算响应与解决时长
    first_response_at: datetime | None = Field(None)
    resolved_at: datetime | None = Field(None)
    replies: list[TicketReply] = Field(default_factory=list, description="人工回复与内部备注")

    @property
    def reason_label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason or "未知")

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABELS.get(self.priority, self.priority)


class TicketUpdate(BaseModel):
    """工单变更请求（只改传入的字段）。"""

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assignee: str | None = None
