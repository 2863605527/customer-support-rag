"""检索抽象层。

两级抽象，职责分明：

- ``VectorStore``：最底层的向量库能力（写入 / 向量检索 / 计数）。只有 Milvus 这类
  自建向量库需要实现它。
- ``RetrievalBackend``：面向业务的**文本检索**接口。调用方只关心「给一个问题文本，
  拿回若干切片」，不关心底层是自建向量库还是 RAGFlow 这类平台。

``VectorStoreBackend`` 负责把 ``VectorStore`` 适配成 ``RetrievalBackend``
（查询文本 -> 向量 -> 检索）。RAGFlow 则在 ``ragfullflow_backend`` 中直接实现
``RetrievalBackend``，因为检索与向量化都在平台侧完成。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from customer_support.models.knowledge import RetrievalHit


class VectorStore(ABC):
    """向量数据库统一接口。"""

    @abstractmethod
    def insert(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        *,
        embedding_fn: Callable[[str], list[float]],
    ) -> int:
        """批量写入文本，返回写入条数。"""

    @abstractmethod
    def search(self, query_vector: list[float], top_k: int = 4) -> list[RetrievalHit]:
        """按查询向量检索。"""

    @abstractmethod
    def count(self) -> int:
        """库中条数。"""

    @abstractmethod
    def drop(self) -> None:
        """清空集合（危险操作）。"""


class RetrievalBackend(ABC):
    """检索后端统一接口（面向文本查询）。"""

    name: str = "base"
    display_name: str = "检索后端"

    @abstractmethod
    def retrieve(self, query: str, top_k: int = 4) -> list[RetrievalHit]:
        """按自然语言查询检索切片，按 score 降序返回。"""

    @abstractmethod
    def count(self) -> int:
        """后端可用切片总数；无法获取时返回 -1。"""

    def fetch_document(self, source: str, *, limit: int = 4) -> list[RetrievalHit]:
        """按**来源文档名**确定性地取回该文档的切片（不走向量相似度）。

        用于「买家贴了商品链接」这类场景：该由哪份文档来回答是**确定**的，
        不该交给相似度去猜——实测同一商品文档在锚点查询下也可能排到别的
        商品之后（跨语言 + 同系列近义标题），导致模型拿不到资料而拒答。

        默认实现返回空列表，表示该后端不支持此能力；上层据此退化为纯相似度
        检索，因此这是纯粹的增强，不会让原本能答的问题变差。
        """
        return []

    def describe(self) -> dict[str, Any]:
        """后端自描述信息，用于 /readyz 与日志。"""
        return {"backend": self.name, "display_name": self.display_name}


class VectorStoreBackend(RetrievalBackend):
    """把自建向量库适配成检索后端。"""

    name = "vector_store"
    display_name = "向量库（自建）"

    def __init__(
        self,
        store: VectorStore,
        embedding_fn: Callable[[str], list[float]],
    ):
        self.store = store
        self.embedding_fn = embedding_fn

    def retrieve(self, query: str, top_k: int = 4) -> list[RetrievalHit]:
        vector = self.embedding_fn(query)
        return self.store.search(vector, top_k=top_k)

    def count(self) -> int:
        return self.store.count()
