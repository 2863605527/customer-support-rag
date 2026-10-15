"""② 问答 Agent：把检索 + 生成收敛成一个可复用的节点。

职责边界（重要）：
- Agent 只做「一次问答」这一步：拿问题（必要时结合会话上下文）→ 检索 → 生成；
- 「要不要转人工、什么时候转」属于业务规则，统一放在 ``services/chat_service.py``，
  避免规则散落在多个 Agent 里导致行为不一致。
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

from customer_support.agents.base import BaseAgent
from customer_support.common.logging import get_logger
from customer_support.rag.generator import AnswerGenerator, GenerationResult

logger = get_logger(__name__)


class QAAgent(BaseAgent):
    """问答 Agent（RAG 回答 + 资料不足判断）。"""

    name = "qa_agent"
    description = "基于知识库生成客服回答，资料不足时标记为需要转人工"

    def __init__(self, generator: AnswerGenerator):
        self.generator = generator

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """执行一次问答。

        state 约定：
        - ``question``: 本轮买家提问（必填）
        - ``rewritten_question``: 结合会话上下文改写后的检索用问题（可选，优先使用）
        - ``language``: 买家语言代码（可选，默认 ja）
        - ``pin_source``: 需要**确定性锚定**的知识库文档名（可选；买家贴了商品链接
          时由上层传入，保证该商品文档一定进入参考资料）
        """
        question: str = state.get("question", "") or ""
        search_query: str = state.get("rewritten_question") or question
        language: str = state.get("language") or "ja"
        pin_source: str | None = state.get("pin_source")

        result = self.generator.answer(search_query, language=language, pin_source=pin_source)
        return {
            "answer": result.answer,
            "source": result.source,
            "references": result.references,
            # 资料不足 -> 需要人工介入
            "need_human": result.source == "no_answer",
            # token 用量（供上游做成本核算与观测）
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            },
        }

    def run_stream(
        self, state: dict[str, Any]
    ) -> Iterator[tuple[str, Any]]:
        """流式版 ``run``：先上报检索进度，再吐回答增量，最后吐终态 state。

        事件序列：
        - ``("status", dict)``：链路阶段进度（检索完成 → 进入生成，含命中条数）；
        - ``("delta", str)``：回答增量；
        - ``("final", dict)``：与 ``run`` 返回结构一致的终态（必发，最后一条）。

        模型拒答（资料不足）时增量内容会被终态的兜底话术**整段替换**
        （``need_human=True``），上层应把已流出的文本替换为终态 answer。
        """
        question: str = state.get("question", "") or ""
        search_query: str = state.get("rewritten_question") or question
        language: str = state.get("language") or "ja"
        pin_source: str | None = state.get("pin_source")

        for kind, payload in self.generator.answer_stream(
            search_query, language=language, pin_source=pin_source
        ):
            if kind == "retrieved":
                hits = cast("list[object]", payload)
                yield ("status", {"stage": "generate", "hits": len(hits)})
                continue
            if kind == "delta":
                yield ("delta", payload)
                continue
            result = cast(GenerationResult, payload)
            yield (
                "final",
                {
                    "answer": result.answer,
                    "source": result.source,
                    "references": result.references,
                    "need_human": result.source == "no_answer",
                    "usage": {
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                    },
                },
            )
