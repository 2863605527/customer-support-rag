"""服务端会话与消息模型（跨设备、可复盘）。

前端把消息放 localStorage 只能解决「同一台浏览器刷新不丢」，企业场景还需要：

- 换电脑/手机也能看到历史（数据在服务端）；
- 值班主管能翻看某通对话的完整上下文与命中引用；
- 统计「会话数 / 自助解决率 / 平均轮次」等运营指标。

``StoredMessage`` 保留 ``trace_id`` 与引用，便于从任一回答反查到日志与知识库来源。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from customer_support.models.message import Reference


class StoredMessage(BaseModel):
    """落库的一条消息（买家提问或客服回答）。"""

    id: int = Field(0)
    session_id: str = Field("")
    role: str = Field(..., description="buyer | agent")
    text: str = Field("")
    intent: str = Field("unknown")
    need_human: bool = Field(False)
    human_reason: str = Field("")
    source: str = Field("")
    confidence: float = Field(0.0)
    ticket_id: str | None = Field(None)
    trace_id: str = Field("")
    latency_ms: int = Field(0)
    references: list[Reference] = Field(default_factory=list)
    created_at: datetime = Field(...)


class ConversationSummary(BaseModel):
    """会话列表项（不含全部消息，便于分页展示）。"""

    session_id: str = Field(...)
    buyer_id: str = Field("")
    language: str = Field("unknown")
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)
    message_count: int = Field(0)
    handoff_count: int = Field(0)
    last_question: str = Field("", description="最近一条买家提问（列表预览用）")
    ticket_ids: list[str] = Field(default_factory=list, description="该会话产生的工单号")


class ConversationDetail(ConversationSummary):
    """会话详情：摘要 + 全部消息。"""

    messages: list[StoredMessage] = Field(default_factory=list)
    total: int = Field(0, description="会话总数（分页用）")
