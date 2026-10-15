"""回答生成器：检索资料 + LLM → 客服回答。

防幻觉设计（三道闸门）：

1. **检索阈值**（在 ``Retriever``）：低于阈值的切片直接丢弃，过滤后为空即「资料不足」；
2. **只依据资料**：prompt 明确要求「只能使用【参考资料】中的信息」，禁止用常识补全；
3. **模型拒答也要被识别**：即使检索有命中，模型仍可能判定「资料里没有这条答案」，
   此时要求它输出哨兵 ``NO_ANSWER``（而不是自由发挥）；生成层据此把 ``source``
   置为 ``no_answer``，交由上层转人工。

⚠️ 第 3 条是曾经的真实漏洞：旧实现只在「检索为空」时才判资料不足，模型按 prompt
输出兜底话术后系统仍标记 ``knowledge`` 且不转人工——出现「话术说要转人工、实际没转」
的不一致（检索命中但答不上来的问题必踩）。现在模型拒答会被确定性识别
（见 ``_is_no_answer``）。

``source`` 字段区分 ``knowledge`` / ``no_answer``，让「这条回答到底有没有依据」
在日志、API 响应和工单里都可见——这是客服系统可复盘的基础。
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from customer_support.common.logging import get_logger
from customer_support.common.resilience import guarded
from customer_support.models.knowledge import RetrievalHit
from customer_support.models.message import Reference
from customer_support.rag.retriever import Retriever

logger = get_logger(__name__)

# 模型判定「资料里没有答案」时要求其输出的哨兵字符串。
# 用哨兵而不是「匹配整段兜底话术」，是为了让判定确定、可测、跨模型稳定。
NO_ANSWER_SENTINEL = "NO_ANSWER"

# 兜底话术：知识库没有答案时使用（按语言给出，默认日语）
FALLBACK_ANSWERS: dict[str, str] = {
    "ja": (
        "申し訳ございません。こちらの件について、現在ご案内できる情報がございません。"
        "担当者に引き継ぎ、あらためてご案内いたします。少々お待ちください。"
    ),
    "en": (
        "I'm sorry, I don't have reliable information about this yet. "
        "Let me transfer you to our support team so they can help you properly."
    ),
    "zh": "非常抱歉，目前没有查到可以确认的信息。我将把您的问题转给人工客服跟进处理。",
    "default": (
        "I'm sorry, I don't have reliable information about this yet. "
        "Let me transfer you to our support team so they can help you properly."
    ),
}

# 买家语言代码 → prompt 里的语言要求描述
LANGUAGE_HINTS: dict[str, str] = {
    "ja": "日本語（丁寧な敬語。海外のお客様向けの自然なビジネス日本語）",
    "en": "English (natural, friendly customer-support tone)",
    "es": "español (tono amable y profesional)",
    "fr": "français (ton courtois et professionnel)",
    "de": "Deutsch (freundlicher, professioneller Ton)",
    "ko": "한국어 (정중한 존댓말)",
    "zh": "简体中文（礼貌、专业）",
}

QA_PROMPT = """あなたは「{shop_name}」のカスタマーサポート担当です。海外の買家からの問い合わせに回答します。

【買家の質問】
{question}

【参考資料（社内ナレッジベース）】
{context}

【回答ルール】
1. 【参考資料】に書かれている情報だけを使って回答してください。書かれていないことは推測・創作しないこと。
2. 【参考資料】に答えが見つからない場合は、この指示を最優先し、他の文言を一切付けずに次の文字列だけを返してください：{sentinel}
3. （2 に該当しない場合）友だちのように親切・簡潔に。3〜4文以内。
4. {language_hint} で回答してください。
5. 価格・サイズ・素材などの数値は参考資料の表記を正確に引用すること。曖昧にぼかさないこと。

回答："""


class GenerationResult(BaseModel):
    """一次回答生成的结果。"""

    answer: str
    source: str = Field("knowledge", description="knowledge / no_answer")
    references: list[Reference] = Field(default_factory=list)
    # token 用量：供应商返回真实 usage 时用真实值，否则按字符数估算（见 _estimate_tokens）
    prompt_tokens: int = Field(0, description="输入 token 数（估算时也是估算值）")
    completion_tokens: int = Field(0, description="输出 token 数")


class AnswerGenerator:
    """RAG 回答生成器。"""

    def __init__(
        self,
        retriever: Retriever,
        llm: BaseChatModel,
        shop_name: str = "くらし寝具",
        max_reference_snippet: int = 200,
    ):
        self.retriever = retriever
        self.llm = llm
        self.shop_name = shop_name
        self.max_reference_snippet = max_reference_snippet

    def fallback(self, language: str) -> str:
        """取对应语言的兜底话术。"""
        return FALLBACK_ANSWERS.get(language, FALLBACK_ANSWERS["default"])

    def answer(
        self, question: str, language: str = "ja", pin_source: str | None = None
    ) -> GenerationResult:
        """生成回答。

        Args:
            question: 买家提问原文
            language: 买家语言代码（决定兜底话术与回答语言）
            pin_source: 需要**确定性锚定**的文档名（买家贴了商品链接时由上层传入，
                保证该商品文档一定进入参考资料，不依赖相似度是否排得上来）

        Returns:
            GenerationResult：answer / source / references
        """
        # 检索与生成分别受各自的熔断器保护：检索后端挂掉不该被算作「LLM 故障」
        with guarded("retrieval"):
            hits = self.retriever.retrieve(question, pin_source=pin_source)
        if not hits:
            logger.info("知识库无相关内容，走兜底话术（language=%s）", language)
            return GenerationResult(answer=self.fallback(language), source="no_answer", references=[])

        context = "\n---\n".join(
            f"[資料{i}] 出典：{h.source or 'unknown'}\n内容：{h.text}"
            for i, h in enumerate(hits, 1)
        )
        prompt = QA_PROMPT.format(
            shop_name=self.shop_name,
            question=question,
            context=context,
            sentinel=NO_ANSWER_SENTINEL,
            language_hint=LANGUAGE_HINTS.get(language, LANGUAGE_HINTS["en"]),
        )
        # 生成调用受 LLM 熔断器保护：连续失败（欠费/超时）后快速失败，不再撞墙
        with guarded("llm", health=True):
            resp = self.llm.invoke(prompt)
        answer = (getattr(resp, "content", "") or "").strip()
        prompt_tokens, completion_tokens = self._extract_usage(resp)
        prompt_tokens, completion_tokens = self._finalize_usage(
            prompt, answer, prompt_tokens, completion_tokens
        )

        references = self._build_references(hits)

        # 检索有命中，但模型判定资料里没有答案 -> 同样按「资料不足」处理，交上层转人工。
        # 保留 references 供工单复盘（「检索到了这些，但都不足以回答」）。
        if self._is_no_answer(answer, language):
            logger.info("模型判定资料不足（未生成有效回答），走兜底话术（language=%s）", language)
            return GenerationResult(
                answer=self.fallback(language),
                source="no_answer",
                references=references,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        logger.info("生成回答完成：%d 字，引用 %d 条资料", len(answer), len(references))
        return GenerationResult(
            answer=answer,
            source="knowledge",
            references=references,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def answer_stream(
        self, question: str, language: str = "ja", pin_source: str | None = None
    ) -> Iterator[tuple[str, object]]:
        """流式版 ``answer``：边生成边吐出增量，供 SSE 逐字输出。

        事件序列（元组首元素为类型）：
        - ``("retrieved", list[RetrievalHit])``：检索完成（含空结果），上层据此
          上报「检索知识库 → 命中 N 条」的阶段进度；
        - ``("delta", str)``：回答增量，按模型产出顺序给出；
        - ``("final", GenerationResult)``：终态结果（最后一个事件，必发）。

        防幻觉闸门不变：拒答判定 ``_is_no_answer`` 只能在**完整文本**上做，
        因此流式期间若模型输出了哨兵/空文本，终态事件会给出兜底话术与
        ``source="no_answer"``，由上层把已流出的内容**整段替换**为兜底话术
        （前端收到 ``replace`` 事件后覆盖气泡文本）。

        Args:
            question: 买家提问原文
            language: 买家语言代码（决定兜底话术与回答语言）
            pin_source: 需要确定性锚定的文档名（买家贴了商品链接时由上层传入）

        Yields:
            ("retrieved", 命中列表) / ("delta", 增量文本) / ("final", GenerationResult)
        """
        with guarded("retrieval"):
            hits = self.retriever.retrieve(question, pin_source=pin_source)
        yield ("retrieved", hits)
        if not hits:
            logger.info("知识库无相关内容，走兜底话术（language=%s）", language)
            yield (
                "final",
                GenerationResult(answer=self.fallback(language), source="no_answer", references=[]),
            )
            return

        context = "\n---\n".join(
            f"[資料{i}] 出典：{h.source or 'unknown'}\n内容：{h.text}"
            for i, h in enumerate(hits, 1)
        )
        prompt = QA_PROMPT.format(
            shop_name=self.shop_name,
            question=question,
            context=context,
            sentinel=NO_ANSWER_SENTINEL,
            language_hint=LANGUAGE_HINTS.get(language, LANGUAGE_HINTS["en"]),
        )

        pieces: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        # 流式生成整体受 LLM 熔断器保护：调用前 acquire，异常时记账（欠费/超时会打开熔断）
        with guarded("llm", health=True):
            for chunk in self.llm.stream(prompt):
                piece = getattr(chunk, "content", "")
                if isinstance(piece, list):  # 兼容多模态内容块
                    piece = "".join(str(p) for p in piece)
                # 部分供应商只在最后一个 chunk 里带 usage；取到就覆盖
                p_tokens, c_tokens = self._extract_usage(chunk)
                if p_tokens or c_tokens:
                    prompt_tokens, completion_tokens = p_tokens, c_tokens
                if not piece:
                    continue
                pieces.append(piece)
                yield ("delta", piece)

        answer = "".join(pieces).strip()
        prompt_tokens, completion_tokens = self._finalize_usage(
            prompt, answer, prompt_tokens, completion_tokens
        )
        references = self._build_references(hits)

        if self._is_no_answer(answer, language):
            logger.info("模型判定资料不足（流式路径），以兜底话术收尾（language=%s）", language)
            yield (
                "final",
                GenerationResult(
                    answer=self.fallback(language),
                    source="no_answer",
                    references=references,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                ),
            )
            return

        logger.info("生成回答完成（流式）：%d 字，引用 %d 条资料", len(answer), len(references))
        yield (
            "final",
            GenerationResult(
                answer=answer,
                source="knowledge",
                references=references,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )

    # ------------------------------------------------------------------
    # token 用量
    # ------------------------------------------------------------------
    # 估算系数：本项目语料以日文/中文为主，1 token ≈ 1.5 个字符（英文约 4）。
    # 供应商返回真实 usage 时优先用真实值；只有拿不到时才估算，并在日志里标明。
    _CHARS_PER_TOKEN = 1.5

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        return max(0, round(len(text) / cls._CHARS_PER_TOKEN))

    @staticmethod
    def _extract_usage(resp: object) -> tuple[int, int]:
        """从模型响应里取真实 token 用量（供应商未返回 usage 时返回 (0, 0)）。"""
        meta = getattr(resp, "response_metadata", None) or {}
        usage = meta.get("token_usage") or meta.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        return prompt_tokens, completion_tokens

    @classmethod
    def _finalize_usage(
        cls, prompt: str, answer: str, prompt_tokens: int, completion_tokens: int
    ) -> tuple[int, int]:
        if not prompt_tokens:
            prompt_tokens = cls._estimate_tokens(prompt)
        if not completion_tokens:
            completion_tokens = cls._estimate_tokens(answer)
        return prompt_tokens, completion_tokens

    def _build_references(self, hits: list[RetrievalHit]) -> list[Reference]:
        """命中切片 → 可溯源的引用（``pinned`` 标出由商品链接确定性锚定的来源）。"""
        return [
            Reference(
                source=h.source,
                doc_id=h.doc_id,
                score=round(h.score, 4),
                snippet=h.text[: self.max_reference_snippet],
                pinned=h.pinned,
            )
            for h in hits
        ]

    @staticmethod
    def _normalize(text: str) -> str:
        """归一化：去掉空白与标点、统一小写，只保留字母（含 CJK / 假名）与数字。"""
        return re.sub(r"[\W_]+", "", text).lower()

    def _is_no_answer(self, answer: str, language: str) -> bool:
        """判断模型本轮是否「拒答」（资料里没有答案）。

        两道防线：
        1. 模型按规则输出约定哨兵 ``NO_ANSWER``（可能带标点或后缀说明，故归一化后按前缀判断）；
        2. 兼容旧行为：模型直接复述了该语言的兜底话术原文。

        空回答也视为拒答——宁转人工，也不把空白回答发给买家。
        """
        if not answer:
            return True
        normalized = self._normalize(answer)
        if normalized.startswith(self._normalize(NO_ANSWER_SENTINEL)):
            return True
        return self._normalize(self.fallback(language)) in normalized
