"""知识库与检索结果模型。"""
from pathlib import Path

from pydantic import BaseModel, Field


class KnowledgeDocument(BaseModel):
    """一份知识库文档（loader 的标准化输出）。"""

    source: Path = Field(..., description="文档路径")
    doc_id: str = Field("", description="文档稳定标识（相对知识库根目录的路径，用于溯源）")
    content: str = Field(..., description="文档全文")
    title: str = Field("", description="文档标题（默认取文件名）")


class KnowledgeChunk(BaseModel):
    """知识库切片（splitter 输出，builder 入库单元）。"""

    text: str = Field(..., min_length=1)
    source: str = Field("", description="来源文档标识")
    chunk_index: int = Field(0, description="在文档中的切片序号")


class RetrievalHit(BaseModel):
    """一次检索命中的切片。"""

    text: str = Field(..., description="切片正文")
    score: float = Field(0.0, description="相似度得分，越大越相关")
    source: str = Field("", description="来源文档名（用于回答溯源）")
    doc_id: str = Field("", description="来源文档的稳定标识")
    metadata: dict = Field(default_factory=dict, description="后端附带的原始元信息")
    pinned: bool = Field(
        False,
        description="是否由商品链接**确定性锚定**（按文档名取回，而非相似度命中）",
    )

    def __str__(self) -> str:  # pragma: no cover - 调试便利
        return f"<hit {self.score:.3f} {self.source} {self.text[:24]!r}>"
