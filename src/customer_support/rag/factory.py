"""检索后端工厂：按配置装配 RAGFlow 或 Milvus。

上层（``Retriever``）只依赖 ``RetrievalBackend`` 接口，因此切换后端是纯配置行为：
``RETRIEVAL_BACKEND=ragflow``（默认）或 ``milvus``。
"""
from __future__ import annotations

from customer_support.common.llm import create_embedding_fn
from customer_support.common.logging import get_logger
from customer_support.config import Settings
from customer_support.rag.base import RetrievalBackend, VectorStoreBackend
from customer_support.rag.ragflow_backend import RagflowBackend

logger = get_logger(__name__)


def build_retrieval_backend(settings: Settings) -> RetrievalBackend:
    """按 ``RETRIEVAL_BACKEND`` 装配检索后端。"""
    backend = settings.retrieval.backend

    if backend == "ragflow":
        rf = settings.ragflow
        logger.info(
            "检索后端 = RAGFlow (%s, datasets=%s)", rf.base_url, ",".join(rf.dataset_ids)
        )
        return RagflowBackend(
            base_url=rf.base_url,
            api_key=rf.api_key,
            dataset_refs=rf.dataset_ids,
            similarity_threshold=rf.similarity_threshold,
            vector_similarity_weight=rf.vector_similarity_weight,
            page_size=rf.page_size,
            timeout=rf.timeout,
            max_retries=rf.max_retries,
            use_rerank=rf.use_rerank,
        )

    if backend == "milvus":
        # 延迟导入：未装 pymilvus 时不影响 RAGFlow 路线
        from customer_support.rag.milvus_store import MilvusStore

        mv = settings.milvus
        logger.info("检索后端 = Milvus (%s:%s, collection=%s)", mv.host, mv.port, mv.collection)
        store = MilvusStore(
            host=mv.host,
            port=mv.port,
            collection_name=mv.collection,
            dim=mv.embedding_dim,
        )
        return VectorStoreBackend(store, create_embedding_fn(settings))

    raise ValueError(f"不支持的检索后端: {backend}")
