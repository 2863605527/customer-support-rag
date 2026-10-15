"""客服会话消息模型。"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Reference(BaseModel):
    """回答引用的一条知识库来源（可溯源，便于复盘与质检）。"""

    source: str = Field("", description="来源文档名")
    doc_id: str = Field("", description="来源文档稳定标识")
    score: float = Field(0.0, description="相似度得分（锚定来源为 1.0，见 pinned）")
    snippet: str = Field("", description="命中切片摘要（截断）")
    pinned: bool = Field(
        False, description="是否由商品链接确定性锚定（而非相似度命中），便于复盘区分"
    )


class ChatMessage(BaseModel):
    """一条买家咨询。

    所有字段都带长度上限：``session_id`` / ``buyer_id`` 会作为 SQLite 主键与内存字典
    的键落库落内存，``language`` 会被拼进 prompt 与日志——不设上限时，
    一个登录用户就能用超长字符串把库和内存撑大（输入校验是边界，不是装饰）。
    """

    question: str = Field(..., min_length=1, max_length=2000)
    session_id: str | None = Field(
        None,
        max_length=128,
        description="会话 ID；传入则启用多轮上下文（买家连续追问时必传）",
    )
    buyer_id: str | None = Field(
        None, max_length=64, description="买家标识（可选，用于工单与统计）"
    )
    language: str | None = Field(
        None, max_length=8, description="买家语言（ja/en/es...，不填则自动识别）"
    )
    received_at: datetime = Field(default_factory=_utcnow)


class ChatResult(BaseModel):
    """一次问答的完整结果。"""

    question: str
    answer: str = Field("", description="客服回答（转人工时为转人工话术）")
    intent: str = Field("unknown", description="意图分类结果")
    need_human: bool = Field(False, description="是否需要转人工")
    human_reason: str = Field(
        "",
        description=(
            "转人工原因：high_risk_intent / low_confidence / no_knowledge / "
            "rate_limited / service_unavailable / system_error"
        ),
    )
    language: str = Field("unknown")
    source: str = Field("", description="回答来源：knowledge / no_answer / human")
    confidence: float = Field(0.0, ge=0, le=1, description="意图置信度")
    references: list[Reference] = Field(default_factory=list, description="引用的知识库来源")
    ticket_id: str | None = Field(None, description="转人工时创建的工单号")
    session_id: str | None = Field(None)
    trace_id: str = Field("", description="链路追踪 ID（与日志中的 trace_id 对应）")
    latency_ms: int = Field(0, description="端到端处理耗时（毫秒）")
    answered_at: datetime = Field(default_factory=_utcnow)

    # ---- 可观测性 / 成本 ----
    cached: bool = Field(False, description="是否命中问答缓存（命中时 latency_ms 置 0）")
    stage_ms: dict[str, int] = Field(
        default_factory=dict,
        description="分阶段耗时（毫秒）：language/rewrite/intent/retrieval+generate",
    )
    prompt_tokens: int = Field(0, description="输入 token 数（供应商未返回时为估算值）")
    completion_tokens: int = Field(0, description="输出 token 数（供应商未返回时为估算值）")
    estimated_cost: float = Field(0.0, description="本次问答估算成本（按配置单价，单位与单价一致）")
