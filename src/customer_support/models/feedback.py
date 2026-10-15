"""回答质量反馈模型（RAG 质量闭环的入口）。

没有反馈，RAG 只能靠"感觉"调优。这里把每条回答的 👍/👎 与原因结构化：

- ``rating``：``up`` / ``down``；
- ``reasons``：预设原因标签（答非所问、信息过时、缺少商品信息、语气问题…），
  用于把差评聚类成可行动的改进项，而不是一句"不好"；
- ``comment``：可选自由文本；
- 同时冗余存 ``question`` / ``answer`` / ``source``，导出后即可直接当**回归评测集**。

``trace_id`` 唯一：同一条回答重复评价只保留最后一次（改主意不产生脏样本）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

Rating = str  # up | down

# 差评原因标签（可扩展）；用于前端做「一键点选」而非让用户打字
DOWN_REASONS: dict[str, str] = {
    "irrelevant": "答非所问",
    "wrong_info": "信息不准确",
    "incomplete": "回答不完整",
    "missing_product": "缺少商品/规格信息",
    "outdated": "信息过时",
    "tone": "语气/表达问题",
    "should_human": "本该转人工",
    "other": "其他",
}

UP_REASONS: dict[str, str] = {
    "accurate": "准确",
    "helpful": "有帮助",
    "fast": "响应快",
    "other": "其他",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FeedbackSubmission(BaseModel):
    """提交一条回答评价。"""

    trace_id: str = Field(..., description="被评价回答的 trace_id（与 ChatResult 一致）")
    rating: str = Field(..., pattern="^(up|down)$", description="up=有帮助 / down=没帮助")
    reasons: list[str] = Field(default_factory=list, description="原因标签（见 UP/DOWN_REASONS）")
    comment: str = Field("", max_length=500, description="可选补充说明")
    session_id: str | None = Field(None)
    question: str = Field("", description="冗余存问题，导出评测集时免去关联查询")
    answer: str = Field("", description="冗余存回答")
    source: str = Field("", description="回答来源（knowledge / no_answer / human）")


class FeedbackRecord(FeedbackSubmission):
    """落库后的反馈记录。"""

    id: int = Field(0)
    created_at: datetime = Field(default_factory=_utcnow)


class FeedbackStats(BaseModel):
    """反馈汇总：好评率是 RAG 质量最直观的单一指标。"""

    total: int = Field(0)
    up: int = Field(0)
    down: int = Field(0)
    positive_rate: float | None = Field(None, description="up / total；无反馈时为 None")
    by_source: dict[str, dict[str, int]] = Field(default_factory=dict)
    by_reason: dict[str, int] = Field(default_factory=dict)
    recent_down: list[FeedbackRecord] = Field(default_factory=list, description="最近差评（供人工跟进）")
