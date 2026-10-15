"""Milvus 向量库实现（备选检索后端，通过 ``RETRIEVAL_BACKEND=milvus`` 启用）。"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from customer_support.common.exceptions import RAGError
from customer_support.common.logging import get_logger
from customer_support.models.knowledge import RetrievalHit
from customer_support.rag.base import VectorStore

logger = get_logger(__name__)


class MilvusStore(VectorStore):
    """基于 Milvus 的向量库。"""

    def __init__(self, host: str, port: int, collection_name: str, dim: int):
        self.collection_name = collection_name
        self.dim = dim
        try:
            connections.connect("default", host=host, port=str(port))
        except Exception as e:
            raise RAGError(f"连接 Milvus 失败 {host}:{port}", detail=str(e)) from e

        if utility.has_collection(collection_name):
            self._collection = Collection(collection_name)
            logger.info("已连接集合: %s", collection_name)
        else:
            self._collection = self._create_collection(collection_name, dim)
            logger.info("已创建集合: %s (dim=%d)", collection_name, dim)

    @staticmethod
    def _create_collection(name: str, dim: int) -> Collection:
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="metadata", dtype=DataType.JSON),
        ]
        schema = CollectionSchema(fields, description="客服知识库")
        collection = Collection(name, schema)
        collection.create_index(
            "vector",
            {"index_type": "IVF_FLAT", "metric_type": "COSINE", "params": {"nlist": 128}},
        )
        return collection

    def insert(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        *,
        embedding_fn: Callable[[str], list[float]],
    ) -> int:
        if not texts:
            return 0
        if metadatas is None:
            metadatas = [{} for _ in texts]
        try:
            vectors = [embedding_fn(t) for t in texts]
            data = [vectors, texts, [json.dumps(m, ensure_ascii=False) for m in metadatas]]
            self._collection.insert(data)
            self._collection.flush()
        except Exception as e:
            raise RAGError("向量入库失败", detail=str(e)) from e
        return len(texts)

    def search(self, query_vector: list[float], top_k: int = 4) -> list[RetrievalHit]:
        try:
            self._collection.load()
            results = self._collection.search(
                data=[query_vector],
                anns_field="vector",
                param={"metric_type": "COSINE", "params": {"nprobe": 16}},
                limit=top_k,
                output_fields=["text", "metadata"],
            )
        except Exception as e:
            raise RAGError("向量检索失败", detail=str(e)) from e
        hits: list[RetrievalHit] = []
        for hit in results[0]:
            meta = hit.entity.get("metadata") or {}
            hits.append(
                RetrievalHit(
                    text=hit.entity.get("text") or "",
                    score=round(float(hit.score), 4),
                    source=str(meta.get("source", "")) if isinstance(meta, dict) else "",
                    doc_id=str(meta.get("doc_id", "")) if isinstance(meta, dict) else "",
                    metadata=meta if isinstance(meta, dict) else {"raw": meta},
                )
            )
        return hits

    def count(self) -> int:
        self._collection.load()
        return self._collection.num_entities

    def drop(self) -> None:
        if utility.has_collection(self.collection_name):
            utility.drop_collection(self.collection_name)
            logger.warning("已删除集合: %s", self.collection_name)
