"""① 意图识别 Agent：分类买家问题，识别高风险转人工。

设计要点：
- LLM 分类，但用「严格 JSON + 枚举值」约束输出，避免自由文本带来的解析不确定性；
- 输出不合规时**只重试一次**（第二次附带纠错提示），仍失败则兜底转人工——
  客服系统里「宁可转人工，不可答错」，兜底方向必须朝安全侧；
- 置信度阈值由配置注入（``AGENT_CONFIDENCE_THRESHOLD``），不在代码里硬编码，
  便于按线上误判率调参。
"""
from __future__ import annotations

import json
import re

from langchain_core.language_models import BaseChatModel

from customer_support.agents.base import BaseAgent
from customer_support.common.logging import get_logger
from customer_support.common.resilience import CircuitOpenError, guarded
from customer_support.models.intent import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    IntentCategory,
    IntentResult,
)

logger = get_logger(__name__)

INTENT_PROMPT = """あなたは越境EC店舗のカスタマーサポート用の意図分類器です。買家の質問がどのカテゴリか判断してください。

【カテゴリ】
- presale: 售前咨询（サイズ、色、発送可能地域、割引など）
- aftersale: 售后问题（返品・交換、品質問題、キズ、部品欠け）
- logistics: 物流问题（追跡番号、到着時期、配送トラブル）
- product_info: 商品信息（素材、仕様、使い方）
- human: 必ず人対応（暴言、クレーム、返金要求、紛争、法律問題など）

【例】
- 商品はいつ届きますか？→ logistics
- サイズを交換したいです → aftersale
- 返品は可能ですか？→ aftersale
- 生地の素材は何ですか？→ product_info
- お金を返してください！→ human
- 注文番号が分かりません、どうすればいいですか？→ logistics

買家がどの言語で質問しても、同じ基準で分類してください。

【買家の質問】
{question}

必ず次の形式の JSON だけを出力してください（前後に説明や Markdown を付けない）：
{{"intent": "カテゴリ名", "confidence": 0.9, "reason": "理由を一文で"}}"""


class IntentAgent(BaseAgent):
    """意图识别 Agent。"""

    name = "intent_agent"
    description = "分类买家咨询意图，识别需转人工的高风险问题"

    def __init__(self, llm: BaseChatModel, confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD):
        self.llm = llm
        self.confidence_threshold = confidence_threshold

    def classify(self, question: str) -> IntentResult:
        """识别单条问题的意图；模型输出不可解析时才兜底转人工。

        「调用失败」与「输出不可解析」必须分开处理，原因见下方两段注释——
        混在一起会让依赖故障被归因成买家高风险诉求。
        """
        prompt = INTENT_PROMPT.format(question=question[:500])
        resp: object | None = None
        for attempt in (1, 2):
            try:
                with guarded("llm", health=True):
                    resp = self.llm.invoke(prompt)
            except CircuitOpenError:
                # 熔断打开：这是「依赖不可用」，不能退化成 low_confidence 兜底——
                # 那样会让复盘数据看不出真实原因。直接上抛，由 ChatService 判 service_unavailable。
                raise
            except Exception as exc:  # noqa: BLE001 - LLM 调用本身失败（超时 / 5xx / 连接错误）
                # 重试由 LLM 客户端内部负责（LLM_MAX_RETRIES），此处再兜底成
                # intent=human 只会把「上游挂了」写成「买家高风险」，且白等一轮超时。
                logger.warning("LLM 调用失败，放弃意图识别（依赖不可用）: %s", exc)
                raise

            # 到这里说明模型确实返回了内容，只有「内容解析不了」才值得重试。
            try:
                data = self._parse_json(getattr(resp, "content", "") or "")
                result = IntentResult(
                    intent=IntentCategory(str(data.get("intent", "")).strip().lower()),
                    confidence=self._parse_confidence(data.get("confidence")),
                    reason=str(data.get("reason", ""))[:200],
                )
                logger.info(
                    "意图识别: intent=%s confidence=%.2f need_human=%s reason=%s",
                    result.intent.value,
                    result.confidence,
                    result.needs_human(self.confidence_threshold),
                    result.reason,
                )
                return result
            except Exception as exc:  # noqa: BLE001 - 解析类异常才兜底
                logger.warning("意图解析失败(第 %d 次): %s | raw=%s", attempt, exc, self._peek(resp))
                if attempt == 2:
                    break
                prompt = (
                    INTENT_PROMPT.format(question=question[:500])
                    + "\n\n注意：前一次输出无法解析为合法 JSON。只输出一行合法 JSON，不要任何其他字符。"
                )

        logger.warning("意图解析连续失败，兜底转人工: %s", question[:50])
        return IntentResult(
            intent=IntentCategory.HUMAN,
            confidence=0.0,
            reason="intent_parse_failed",
            # 标记为兜底：转人工方向不变（保守），但 ChatService 不会把它
            # 归因成 high_risk_intent，而是按低置信度处理。
            fallback=True,
        )

    def run(self, state: dict[str, object]) -> dict[str, object]:
        """LangGraph 节点形式。"""
        question = str(state.get("question", ""))
        result = self.classify(question)
        return {
            "intent": result.intent.value,
            "confidence": result.confidence,
            "need_human": result.needs_human(self.confidence_threshold),
        }

    # ------------------------------------------------------------------
    # 解析辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _peek(resp: object) -> str:
        """取一段模型原始输出用于排障（截断，避免日志膨胀）。"""
        try:
            return str(getattr(resp, "content", ""))[:120].replace("\n", " ")
        except Exception:  # noqa: BLE001
            return "<unavailable>"

    @staticmethod
    def _parse_confidence(raw: object) -> float:
        """容忍 "0.9" / "90%" / 0.9 等写法。"""
        if raw is None:
            return 0.0
        if isinstance(raw, str) and raw.strip().endswith("%"):
            try:
                return max(0.0, min(1.0, float(raw.strip().rstrip("%")) / 100.0))
            except ValueError:
                return 0.0
        if not isinstance(raw, (int, float, str)):
            return 0.0
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _parse_json(text: str) -> dict:
        """从模型输出里抠出第一个 JSON 对象，容忍 ```json 围栏与前后废话。"""
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.lower().startswith("json"):
                stripped = stripped[4:]
        stripped = stripped.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", stripped, re.S)
            if not match:
                raise
            return json.loads(match.group())
