"""pytest 共享夹具：假检索后端、假 LLM、临时知识库目录。

原则：单测不依赖任何外部服务（RAGFlow / Milvus / 真实 LLM API）。
所有外部依赖都在这里用「最小可用实现」替身，接口与真实实现一致。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from customer_support.models.knowledge import RetrievalHit  # noqa: E402
from customer_support.rag.base import RetrievalBackend, VectorStore  # noqa: E402


class FakeRetrievalBackend(RetrievalBackend):
    """内存检索后端：按构造时给定的命中列表返回（可按查询过滤）。"""

    name = "fake"
    display_name = "假检索后端"

    def __init__(self, hits: list[RetrievalHit] | None = None):
        self.hits = list(hits or [])
        self.calls: list[tuple[str, int]] = []
        # 可供「确定性锚定」按文档名取回的存根：{文档名: [切片]}
        self.documents: dict[str, list[RetrievalHit]] = {}
        self.pin_calls: list[tuple[str, int]] = []

    def retrieve(self, query: str, top_k: int = 4) -> list[RetrievalHit]:
        self.calls.append((query, top_k))
        return self.hits[:top_k]

    def fetch_document(self, source: str, *, limit: int = 4) -> list[RetrievalHit]:
        """模拟 RAGFlow 的按名取回（锚定用）；未登记该文档时返回空列表。"""
        self.pin_calls.append((source, limit))
        return self.documents.get(source, [])[:limit]

    def count(self) -> int:
        return len(self.hits)

    def add(self, text: str, score: float, source: str = "doc") -> None:
        self.hits.append(RetrievalHit(text=text, score=score, source=source, doc_id=source))

    def add_document(self, source: str, texts: list[str]) -> None:
        """登记一份可被 ``fetch_document`` 确定性取回的文档（模拟远端知识库）。"""
        self.documents[source] = [
            RetrievalHit(text=t, score=1.0, source=source, doc_id=source, pinned=True)
            for t in texts
        ]


class FakeVectorStore(VectorStore):
    """内存向量库（VectorStore 接口最小实现）。"""

    def __init__(self):
        self._items: list[dict[str, Any]] = []

    def insert(self, texts, metadatas=None, *, embedding_fn=None) -> int:
        if metadatas is None:
            metadatas = [{} for _ in texts]
        for t, m in zip(texts, metadatas, strict=False):
            self._items.append({"text": t, "metadata": m, "score": 1.0})
        return len(texts)

    def search(self, query_vector, top_k=4) -> list[RetrievalHit]:
        return [
            RetrievalHit(
                text=it["text"],
                score=float(it["score"]),
                source=str(it["metadata"].get("source", "")),
                doc_id=str(it["metadata"].get("doc_id", "")),
                metadata=it["metadata"],
            )
            for it in self._items[:top_k]
        ]

    def count(self) -> int:
        return len(self._items)

    def drop(self) -> None:
        self._items.clear()


class FakeLLM:
    """Mock LLM：按顺序返回预置 payload；用完后重复最后一个。

    ``payloads`` 可以是单个字符串，或字符串列表（多轮调用时依次取用）。
    """

    def __init__(self, payloads: str | list[str]):
        self.payloads: list[str] = [payloads] if isinstance(payloads, str) else list(payloads)
        self.calls: list[str] = []

    def invoke(self, prompt: str):
        self.calls.append(prompt)
        idx = min(len(self.calls) - 1, len(self.payloads) - 1)
        payload = self.payloads[idx]

        class _Resp:
            def __init__(self, content: str):
                self.content = content

        return _Resp(payload)

    def stream(self, prompt: str):
        """流式接口：把 payload 按小块吐出（模拟 token 增量）。"""
        self.calls.append(prompt)
        idx = min(len(self.calls) - 1, len(self.payloads) - 1)
        payload = self.payloads[idx]
        # 按 3 字符一切块，保证「多块拼接 == 完整 payload」
        for i in range(0, len(payload), 3):
            class _Chunk:
                def __init__(self, content: str):
                    self.content = content

            yield _Chunk(payload[i : i + 3])


@pytest.fixture
def fake_backend() -> FakeRetrievalBackend:
    return FakeRetrievalBackend()


@pytest.fixture
def fake_store() -> FakeVectorStore:
    return FakeVectorStore()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM('{"intent": "logistics", "confidence": 0.9, "reason": "测试"}')


@pytest.fixture
def tmp_knowledge_dir(tmp_path: Path) -> Path:
    """临时知识库目录，含一份商品文档 + 一份政策文档 + 一个应被跳过的 README。"""
    kb = tmp_path / "knowledge_base"
    (kb / "products").mkdir(parents=True)
    (kb / "policies").mkdir(parents=True)
    (kb / "products" / "KB-0001.md").write_text(
        "# 綿100% 掛け布団カバー\n\n## 価格\n- 販売価格：**5,000円**（税込）\n", encoding="utf-8"
    )
    (kb / "policies" / "after_sales_policy.md").write_text(
        "# 返品ポリシー\n\n- 到着後30日以内は返品可能です。\n", encoding="utf-8"
    )
    (kb / "README.md").write_text("# 说明文件，不应入库\n", encoding="utf-8")
    return kb
