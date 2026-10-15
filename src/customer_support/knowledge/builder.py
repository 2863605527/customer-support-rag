"""知识库构建器：加载 → 切片 → 向量化 → 入库。"""
from __future__ import annotations

from collections.abc import Callable

from customer_support.common.logging import get_logger
from customer_support.knowledge.loader import load_knowledge_documents, to_langchain_documents
from customer_support.knowledge.splitter import split_documents
from customer_support.rag.base import VectorStore

logger = get_logger(__name__)


def build_knowledge_base(
    knowledge_dir,
    vector_store: VectorStore,
    embedding_fn: Callable[[str], list[float]],
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> int:
    """构建知识库，返回入库切片数。"""
    # 1. 加载文档
    docs = load_knowledge_documents(knowledge_dir)
    if not docs:
        logger.warning("没有加载到任何文档，请检查目录内容")
        return 0

    # 2. 切片
    chunks = split_documents(
        to_langchain_documents(docs), chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )

    # 3. 入库（每条带来源元信息）
    texts = [c.page_content for c in chunks]
    metadatas = [
        {"source": (c.metadata or {}).get("source", "unknown")} for c in chunks
    ]
    n = vector_store.insert(texts, metadatas, embedding_fn=embedding_fn)
    logger.info("知识库构建完成，共 %d 条", n)
    return n
