"""知识库构建脚本（等价于 CLI 的 build-kb）。

用法：
    python scripts/build_kb.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from customer_support.common.llm import create_embedding_fn  # noqa: E402
from customer_support.common.logging import setup_logging  # noqa: E402
from customer_support.config import get_settings  # noqa: E402
from customer_support.knowledge.builder import build_knowledge_base  # noqa: E402
from customer_support.rag.milvus_store import MilvusStore  # noqa: E402


def main() -> None:
    setup_logging()
    settings = get_settings()
    store = MilvusStore(
        host=settings.milvus.host,
        port=settings.milvus.port,
        collection_name=settings.milvus.collection,
        dim=settings.milvus.embedding_dim,
    )
    n = build_knowledge_base(
        knowledge_dir=settings.knowledge.knowledge_base_dir,
        vector_store=store,
        embedding_fn=create_embedding_fn(settings),
        chunk_size=settings.knowledge.chunk_size,
        chunk_overlap=settings.knowledge.chunk_overlap,
    )
    print(f"✅ 知识库构建完成，共 {n} 条。")


if __name__ == "__main__":
    main()
