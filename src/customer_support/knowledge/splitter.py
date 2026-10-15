"""文本切片器：长文档 → 语义完整的检索单元。

设计要点：
- 优先按段落/句子边界切（RecursiveCharacterTextSplitter）
- 切片大小直接影响检索精度：过大不精准，过小上下文不足
- 重叠避免语义在边界被切断
"""
from __future__ import annotations

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from customer_support.common.logging import get_logger

logger = get_logger(__name__)


def split_documents(
    docs: list[Document],
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> list[Document]:
    """把 langchain Document 列表切成检索切片。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", ". ", " ", ""],  # 中文优先按句号切
        length_function=len,
    )
    chunks = splitter.split_documents(docs)
    logger.info("切片完成：%d 文档 -> %d 切片", len(docs), len(chunks))
    return chunks
