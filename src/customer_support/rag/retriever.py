"""检索器：问题 → 检索后端 → 阈值过滤 → 来源去重 → 上下文。

这是防幻觉的第一道防线，也是「资料不足就转人工」这条业务规则的触发器：

1. **确定性锚定**（可选）：买家贴了商品链接或商品番号时，「该由哪份文档来回答」
   是**确定**的——按文档名直接取回该商品文档的切片并置顶，不交给相似度去猜。
   实测同一个商品文档在锚点查询下也可能排到别的商品之后（跨语言 + 同系列近义标题），
   模型因此拿不到资料而拒答。相似度检索只用于补足剩余名额，且不再从该来源取材；
   锚定取不到时（后端不支持 / 远端无此文档）自动退化为纯相似度检索——纯增强，
   不会让原本能答的问题变差。
2. **超取**：向后端多要一些候选（默认 2 倍 top_k），给过滤留冗余；
3. **阈值过滤**：低于 ``score_threshold`` 的切片直接丢弃——过滤后为空即视为
   「知识库没有这条信息」，由上层走兜底话术并转人工，而不是让模型硬答；
4. **来源去重**：同一份文档最多保留 ``max_per_source`` 条切片，避免一个长文档
   霸占全部上下文（提升覆盖度，也让引用来源更可读）。锚定文档不占该配额——
   它的切片是按名取回、有意全给的。
"""
from __future__ import annotations

from collections import Counter

from customer_support.common.logging import get_logger
from customer_support.models.knowledge import RetrievalHit
from customer_support.rag.base import RetrievalBackend

logger = get_logger(__name__)


class Retriever:
    """基于检索后端的知识检索器。"""

    def __init__(
        self,
        backend: RetrievalBackend,
        top_k: int = 4,
        # 与 config.RetrievalSettings.score_threshold 对齐（0.53 为本知识库标定值）；
        # 生产路径由 factory/deps 显式传入配置值，此处默认值只服务直接构造的场景。
        score_threshold: float = 0.53,
        max_per_source: int = 2,
    ):
        self.backend = backend
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.max_per_source = max(1, max_per_source)

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        pin_source: str | None = None,
    ) -> list[RetrievalHit]:
        """检索并过滤，返回按相关度排序的切片。

        Args:
            query: 检索语句
            top_k: 覆盖实例默认的 top_k（上下文预算）
            pin_source: 需要**确定性锚定**的文档名（如买家贴了商品链接 →
                ``KB-0001.md``）。命中时该文档的切片无条件置顶，相似度检索只
                用于补足剩余名额，且不再从该来源取材（避免同一份文档挤占预算）。
        """
        k = top_k or self.top_k
        pinned = self._pin(pin_source, k) if pin_source else []
        pinned_sources = {h.source or h.doc_id for h in pinned}

        # 锚定已占满预算就不必再打检索后端
        remaining = k - len(pinned)
        raw = self.backend.retrieve(query, top_k=k * 2) if remaining > 0 else []
        passed = [h for h in raw if h.score >= self.score_threshold]

        if not passed and not pinned:
            logger.info(
                "检索命中 %d 条，全部低于阈值 %.2f -> 判定资料不足",
                len(raw), self.score_threshold,
            )
            return []

        # 来源去重：同一文档最多保留 max_per_source 条（已按分数降序）。
        # 锚定文档不参与该配额——它的切片是按名取回、有意全给的。
        per_source: Counter[str] = Counter()
        picked: list[RetrievalHit] = list(pinned)
        for hit in passed:
            key = hit.source or hit.doc_id or "unknown"
            if key in pinned_sources:
                continue  # 锚定文档已完整给出，不再重复取材
            if per_source[key] >= self.max_per_source:
                continue
            per_source[key] += 1
            picked.append(hit)
            if len(picked) >= k:
                break

        logger.info(
            "检索命中 %d 条（锚定 %d 条），阈值过滤后 %d 条，采用 %d 条（来源 %d 个）",
            len(raw), len(pinned), len(passed), len(picked),
            len(per_source) + len(pinned_sources),
        )
        return picked[:k]

    def build_context(self, query: str, top_k: int | None = None) -> str:
        """组装喂给 LLM 的上下文；无有效结果返回空串。"""
        hits = self.retrieve(query, top_k)
        if not hits:
            return ""
        blocks = [
            f"[資料{i}] 出典：{h.source or 'unknown'}\n内容：{h.text}"
            for i, h in enumerate(hits, 1)
        ]
        return "\n---\n".join(blocks)

    def _pin(self, pin_source: str, k: int) -> list[RetrievalHit]:
        """按文档名锚定取回切片。

        锚定是**增强**而非依赖：任何失败都只记日志并退化为相似度检索，
        绝不因为「取不到锚定文档」就把买家的提问判成失败。
        """
        try:
            hits = self.backend.fetch_document(pin_source, limit=k)
        except Exception as exc:  # noqa: BLE001 - 锚定失败退化为纯相似度检索
            logger.warning("锚定文档失败（source=%s），退化为相似度检索：%s", pin_source, exc)
            return []
        if hits:
            logger.info("已锚定文档 %s：确定性取回 %d 条切片", pin_source, len(hits))
        else:
            logger.info("锚定文档 %s 未命中（远端无此文档或后端不支持），走相似度检索", pin_source)
        return hits
