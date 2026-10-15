"""运营控制台的请求 / 响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from customer_support.models.conversation import ConversationDetail, ConversationSummary
from customer_support.models.ticket import Ticket, TicketReply


class TicketPage(BaseModel):
    """工单分页结果。"""

    items: list[Ticket] = Field(default_factory=list)
    total: int = Field(0, description="符合筛选条件的总数（不是当页条数）")
    limit: int = Field(50)
    offset: int = Field(0)


class ConversationPage(BaseModel):
    """会话分页结果。"""

    items: list[ConversationSummary] = Field(default_factory=list)
    total: int = Field(0)
    limit: int = Field(30)
    offset: int = Field(0)


class ReplyRequest(BaseModel):
    """新增工单回复 / 内部备注。"""

    body: str = Field(..., min_length=1, max_length=4000)
    internal: bool = Field(False, description="True=内部备注（不发给买家）")


class SearchProbeRequest(BaseModel):
    """在线检索测试请求（调阈值/看命中，无需命令行）。"""

    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(8, ge=1, le=30)
    score_threshold: float = Field(0.0, ge=0.0, le=1.0, description="0=不过滤，便于观察完整排名")


class SearchProbeHit(BaseModel):
    source: str = Field("")
    doc_id: str = Field("")
    score: float = Field(0.0)
    text: str = Field("")


class SearchProbeResult(BaseModel):
    query: str = Field("")
    hits: list[SearchProbeHit] = Field(default_factory=list)
    total: int = Field(0, description="返回条数")
    threshold_passed: int = Field(0, description="按当前线上阈值过滤后仍保留的条数")


class KnowledgeDocument(BaseModel):
    """RAGFlow 远端文档（运维视角）。"""

    id: str = Field("")
    name: str = Field("")
    size: int = Field(0)
    chunk_count: int = Field(0)
    run: str = Field("", description="解析状态：DONE / RUNNING / FAIL / …")
    status: str = Field("")


class ReingestRequest(BaseModel):
    """触发重新入库。"""

    replace: bool = Field(False, description="同名文档重新上传（内容变更时用）")
    prune: bool = Field(False, description="删除本地已不存在的远端文档（删除前自动备份）")
    wait: bool = Field(False, description="是否等待解析完成（大库建议 False，后台慢慢跑）")


class ReingestResult(BaseModel):
    accepted: bool = Field(False)
    message: str = Field("")
    uploaded: int = Field(0)
    skipped: int = Field(0)
    deleted: int = Field(0)
    failed: int = Field(0)


__all__ = [
    "ConversationDetail",
    "ConversationPage",
    "ConversationSummary",
    "KnowledgeDocument",
    "ReingestRequest",
    "ReingestResult",
    "ReplyRequest",
    "SearchProbeHit",
    "SearchProbeRequest",
    "SearchProbeResult",
    "TicketPage",
    "TicketReply",
]
