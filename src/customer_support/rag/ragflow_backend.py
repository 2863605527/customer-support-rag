"""RAGFlow 检索后端：把 RAGFlow 知识库接入本项目的检索抽象。

为什么用 RAGFlow 而不是自己维护向量库：

- 文档解析（DeepDOC）与分块、向量化、混合检索（向量 + 关键词）都由平台承担，
  业务侧只负责「问什么、拿回来的东西够不够回答问题」；
- 知识库文档可以上传后在线增删改，运维不需要重启应用；
- 本地只需一份 API Key，不需要暴露向量库端口。

关键设计：
- **名称与 ID 都支持**：``dataset_ids`` 里可以写知识库名称，首次调用时惰性解析成真实 ID，
  避免了「换个环境要手抄 ID」的运维负担；
- **二次阈值过滤**：RAGFlow 侧用 ``similarity_threshold`` 做粗筛（默认 0.15 较宽松），
  本项目在 ``Retriever`` 里按 ``RETRIEVAL_SCORE_THRESHOLD`` 再精筛一次，
  保证「资料不足 -> 转人工」这条防幻觉链路不被平台默认值影响；
- **失败可降级**：网络/鉴权失败抛出 ``ExternalAPIError``，由上层决定是否降级为转人工，
  不会静默返回空结果让客服系统误判为「知识库没有这条」。
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from customer_support.common.exceptions import ConfigError, ExternalAPIError
from customer_support.common.logging import get_logger
from customer_support.models.knowledge import RetrievalHit
from customer_support.rag.base import RetrievalBackend

logger = get_logger(__name__)

# 需要重试的 HTTP 状态码（限流与瞬时故障）
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RagflowBackend(RetrievalBackend):
    """基于 RAGFlow 检索接口的知识库后端。"""

    name = "ragflow"
    display_name = "RAGFlow 知识库"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        dataset_refs: list[str] | None = None,
        *,
        similarity_threshold: float = 0.15,
        # 与 config.RAGFlowSettings.vector_similarity_weight 对齐（0.7 为本知识库实测最优）
        vector_similarity_weight: float = 0.7,
        page_size: int = 10,
        timeout: float = 30.0,
        max_retries: int = 3,
        use_rerank: bool = False,
        client: httpx.Client | None = None,
    ):
        if not api_key:
            raise ConfigError("未配置 RAGFLOW_API_KEY，无法启用 RAGFlow 检索后端")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dataset_refs = list(dataset_refs or [])
        self.similarity_threshold = similarity_threshold
        self.vector_similarity_weight = vector_similarity_weight
        self.page_size = page_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.use_rerank = use_rerank
        self._client = client
        self._dataset_ids: list[str] | None = None
        # 文档名 → (dataset_id, document_id) 映射，带 TTL 缓存：每次问答都拉一遍
        # 文档列表太浪费，而文档增删是运维级低频事件（见 fetch_document）。
        self._doc_index: dict[str, tuple[str, str]] | None = None
        self._doc_index_at: float = 0.0

    # ------------------------------------------------------------------
    # HTTP 基础设施
    # ------------------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """带重试的请求，统一解包 RAGFlow 的 ``{code, message, data}`` 信封。"""
        last_error: str = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "RAGFlow 请求异常(%d/%d) %s %s -> %s",
                    attempt, self.max_retries, method, path, last_error,
                )
                if attempt == self.max_retries:
                    break
                time.sleep(min(2.0 * attempt, 6.0))
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                last_error = f"HTTP {resp.status_code}"
                logger.warning(
                    "RAGFlow 返回可重试状态(%d/%d) %s -> HTTP %d",
                    attempt, self.max_retries, path, resp.status_code,
                )
                time.sleep(min(2.0 * attempt, 6.0))
                continue

            try:
                payload = resp.json()
            except ValueError:
                raise ExternalAPIError(
                    "RAGFlow 返回非 JSON 响应",
                    detail=f"HTTP {resp.status_code}: {resp.text[:200]}",
                ) from None

            if resp.status_code >= 400:
                raise ExternalAPIError(
                    f"RAGFlow 请求失败 HTTP {resp.status_code}",
                    detail=str(payload)[:300],
                )

            code = payload.get("code")
            if code not in (0, None):
                raise ExternalAPIError(
                    f"RAGFlow 业务错误 code={code}",
                    detail=str(payload.get("message"))[:300],
                    remote_code=int(code) if isinstance(code, int) else None,
                )
            return payload.get("data") or {}

        raise ExternalAPIError("RAGFlow 请求重试耗尽", detail=last_error)

    # ------------------------------------------------------------------
    # 知识库解析
    # ------------------------------------------------------------------
    def _list_datasets(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v1/datasets", params={"page": 1, "page_size": 100})
        return data if isinstance(data, list) else (data.get("data") or [])

    def resolve_dataset_ids(self, refresh: bool = False) -> list[str]:
        """把配置里的知识库名称解析成 dataset id（惰性、带缓存）。"""
        if self._dataset_ids is not None and not refresh:
            return self._dataset_ids

        datasets = self._list_datasets()
        by_name = {d.get("name"): d.get("id") for d in datasets}
        by_id = {d.get("id") for d in datasets}

        resolved: list[str] = []
        missing: list[str] = []
        for ref in self.dataset_refs:
            if ref in by_id:
                resolved.append(ref)
            elif ref in by_name:
                resolved.append(str(by_name[ref]))
            else:
                missing.append(ref)

        if missing:
            available = ", ".join(sorted(n for n in by_name if n)) or "(空)"
            raise ConfigError(
                f"RAGFlow 中找不到知识库: {', '.join(missing)}",
                detail=f"当前账号可见的知识库: {available}",
            )
        if not resolved:
            raise ConfigError("未配置 RAGFLOW_DATASET_IDS，无法确定要检索哪个知识库")

        self._dataset_ids = resolved
        logger.info("RAGFlow 知识库解析完成: %s -> %s", self.dataset_refs, resolved)
        return resolved

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def retrieve(self, query: str, top_k: int = 4) -> list[RetrievalHit]:
        """调用 RAGFlow 召回分块，并归一化为 RetrievalHit。"""
        if not query.strip():
            return []

        dataset_ids = self.resolve_dataset_ids()
        body: dict[str, Any] = {
            "question": query,
            "dataset_ids": dataset_ids,
            "page": 1,
            # 多取一些，留给上层做阈值过滤与去重
            "page_size": max(top_k * 2, self.page_size),
            "similarity_threshold": self.similarity_threshold,
            "vector_similarity_weight": self.vector_similarity_weight,
            "knn_top_k": 1024,
            "keyword": False,
            "highlight": False,
            "use_kg": False,
        }
        data = self._request("POST", "/api/v1/retrieval", json=body)
        chunks = data.get("chunks") or []
        if self.use_rerank:
            logger.debug("RAGFlow 已按 rerank 配置排序（use_rerank=True）")

        hits: list[RetrievalHit] = []
        for c in chunks:
            content = (c.get("content") or "").strip()
            if not content:
                continue
            doc_name = c.get("document_keyword") or c.get("docnm_kwd") or ""
            hits.append(
                RetrievalHit(
                    text=content,
                    score=float(c.get("similarity") or 0.0),
                    source=doc_name,
                    doc_id=str(c.get("document_id") or ""),
                    metadata={
                        "chunk_id": c.get("id"),
                        "dataset_id": c.get("dataset_id"),
                        "vector_similarity": c.get("vector_similarity"),
                        "term_similarity": c.get("term_similarity"),
                    },
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        logger.info("RAGFlow 召回 %d 条（top_k=%d, 粗筛阈值=%.2f）", len(hits), top_k, self.similarity_threshold)
        return hits[: max(top_k, 1)]

    # ------------------------------------------------------------------
    # 确定性锚定（买家贴商品链接时按文档名取回，不靠相似度碰运气）
    # ------------------------------------------------------------------
    # 文档索引缓存时长：文档增删是运维级低频操作，5 分钟内复用同一份映射足够，
    # 又能在重新入库后自愈（不必重启服务）。
    DOC_INDEX_TTL = 300.0

    @staticmethod
    def _canonical_doc_name(name: str) -> str:
        """归一化文档名：去掉 RAGFlow 同名上传自动加的 "(1)" 副本后缀。

        正常情况（reconcile 已收敛）名字是干净的；这里做兜底，避免历史遗留
        副本让锚定悄悄失效。
        """
        return re.sub(r"\s*\(\d+\)(?=\.[^.]+$)", "", name)

    def _list_documents(self, dataset_id: str) -> list[dict[str, Any]]:
        """列出某知识库的文档（翻页；不信任响应里的 total，以「本页不满」为终止）。"""
        docs: list[dict[str, Any]] = []
        page = 1
        while page <= 50:
            data = self._request(
                "GET",
                f"/api/v1/datasets/{dataset_id}/documents",
                params={"page": page, "page_size": 100},
            )
            batch = (data or {}).get("docs") if isinstance(data, dict) else data
            batch = batch or []
            docs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return docs

    def _document_index(self, refresh: bool = False) -> dict[str, tuple[str, str]]:
        """文档名 → (dataset_id, document_id) 映射（带 TTL 缓存）。"""
        now = time.time()
        if (
            self._doc_index is not None
            and not refresh
            and now - self._doc_index_at < self.DOC_INDEX_TTL
        ):
            return self._doc_index

        index: dict[str, tuple[str, str]] = {}
        for dataset_id in self.resolve_dataset_ids():
            for doc in self._list_documents(dataset_id):
                name = str(doc.get("name") or "").strip()
                doc_id = str(doc.get("id") or "")
                if name and doc_id:
                    index[name] = (dataset_id, doc_id)
        self._doc_index = index
        self._doc_index_at = now
        logger.info("RAGFlow 文档索引刷新：%d 份", len(index))
        return index

    def _list_chunks(
        self, dataset_id: str, document_id: str, *, limit: int
    ) -> list[dict[str, Any]]:
        """取某文档的前 ``limit`` 条切片（按文档内顺序，不做相似度排序）。"""
        chunks: list[dict[str, Any]] = []
        page = 1
        while len(chunks) < limit and page <= 20:
            data = self._request(
                "GET",
                f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks",
                params={"page": page, "page_size": 100},
            )
            batch = (data or {}).get("chunks") if isinstance(data, dict) else data
            batch = batch or []
            chunks.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return chunks[:limit]

    def fetch_document(self, source: str, *, limit: int = 4) -> list[RetrievalHit]:
        """按文档名**确定性**取回该文档的切片（买家贴商品链接时的锚定依据）。

        与 ``retrieve`` 的区别：这里不做相似度排序——「该由哪份文档回答」是确定的。
        名字匹配两步走：先精确匹配，不中再按去副本后缀的规范名匹配。

        失败（远端无此文档 / 请求异常）返回空列表而非抛错：锚定是增强能力，
        取不到就退化为相似度检索，不该让买家收到失败。
        """
        if not source.strip():
            return []
        try:
            index = self._document_index()
            matched: tuple[str, str, str] | None = None
            if source in index:
                dataset_id, document_id = index[source]
                matched = (source, dataset_id, document_id)
            else:
                canonical = self._canonical_doc_name(source)
                for name, (dataset_id, document_id) in index.items():
                    if self._canonical_doc_name(name) == canonical:
                        matched = (name, dataset_id, document_id)
                        break
            if matched is None:
                logger.info("锚定失败：RAGFlow 里找不到文档 %s", source)
                return []
            doc_name, dataset_id, document_id = matched
            chunks = self._list_chunks(dataset_id, document_id, limit=limit)
        except ExternalAPIError as exc:
            logger.warning("锚定文档 %s 时 RAGFlow 请求失败，退化为相似度检索：%s", source, exc)
            return []

        hits = [
            RetrievalHit(
                text=str(chunk.get("content") or "").strip(),
                # 锚定是确定性命中、不来自相似度：用 1.0 表达「必然采用」，
                # 并由 pinned=True 与真实相似度区分（API 引用与工单可据此复盘）。
                score=1.0,
                source=doc_name,
                doc_id=document_id,
                metadata={
                    "chunk_id": chunk.get("id"),
                    "dataset_id": dataset_id,
                    "pinned": True,
                },
                pinned=True,
            )
            for chunk in chunks
            if str(chunk.get("content") or "").strip()
        ]
        logger.info("锚定文档 %s：确定性取回 %d 条切片", doc_name, len(hits))
        return hits

    def count(self) -> int:
        """返回知识库文档总数（RAGFlow 按文档计数）。"""
        try:
            datasets = self._list_datasets()
            ids = set(self.resolve_dataset_ids())
            return sum(int(d.get("document_count") or 0) for d in datasets if d.get("id") in ids)
        except Exception as exc:  # noqa: BLE001 - 计数失败不应阻断服务
            logger.warning("获取 RAGFlow 文档数失败: %s", exc)
            return -1

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "datasets": self.dataset_refs,
            "similarity_threshold": self.similarity_threshold,
            "vector_similarity_weight": self.vector_similarity_weight,
        }

    def health(self) -> dict[str, Any]:
        """连通性自检，供 /readyz 使用。"""
        try:
            ids = self.resolve_dataset_ids(refresh=True)
            return {"ok": True, "dataset_ids": ids}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
