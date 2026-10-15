"""切片器单元测试。"""
from langchain_core.documents import Document

from customer_support.knowledge.splitter import split_documents


class TestSplitDocuments:
    def test_splits_long_doc(self):
        docs = [Document(
            page_content="这是商品说明。\n\n尺码表如下：S/M/L。\n\n售后政策：支持7天无理由退货。",
            metadata={"source": "manual"},
        )]
        chunks = split_documents(docs, chunk_size=10, chunk_overlap=2)
        assert len(chunks) > 1
        # 所有切片拼接应覆盖原文
        joined = "".join(c.page_content for c in chunks)
        assert "尺码表" in joined

    def test_preserves_source_metadata(self):
        docs = [Document(page_content="a" * 100, metadata={"source": "policy"})]
        chunks = split_documents(docs, chunk_size=30, chunk_overlap=5)
        assert all(c.metadata.get("source") == "policy" for c in chunks)
