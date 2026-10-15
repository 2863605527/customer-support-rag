"""会话记忆与多轮指代消解。

真实客服对话很少是一问一答：买家常常先问「この布団カバーは何の素材ですか？」，
再追问「洗濯機で洗えますか？」——第二句脱离上文根本无法检索。

这里提供两件事：

1. ``SessionStore``：进程内会话记忆（带 TTL 与轮数上限）。之所以先用进程内实现：
   跨境店铺内部工具的并发量不需要 Redis，而进程内实现没有运维成本；接口足够抽象，
   后续换 Redis 只需再实现一个同名类。
2. ``QueryRewriter``：把带指代的追问改写成自包含的检索语句。**失败即降级**——
   改写失败时直接用原问题检索，绝不因为「改写这一步挂了」让整通对话失败。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel

from customer_support.common.logging import get_logger
from customer_support.common.resilience import guarded

logger = get_logger(__name__)


@dataclass
class Turn:
    """一轮对话。"""

    question: str
    answer: str = ""
    intent: str = "unknown"
    need_human: bool = False
    ts: float = field(default_factory=time.time)


class SessionStore:
    """带 TTL 与容量上限的进程内会话记忆。"""

    def __init__(self, ttl_seconds: int = 3600, max_turns: int = 4, max_sessions: int = 5000):
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self.max_sessions = max_sessions
        self._lock = threading.Lock()
        self._data: OrderedDict[str, list[Turn]] = OrderedDict()

    def get(self, session_id: str | None) -> list[Turn]:
        """取会话历史（过期会话直接淘汰）。"""
        if not session_id:
            return []
        now = time.time()
        with self._lock:
            turns = self._data.get(session_id)
            if not turns:
                return []
            if now - turns[-1].ts > self.ttl_seconds:
                self._data.pop(session_id, None)
                return []
            self._data.move_to_end(session_id)
            return list(turns)

    def append(self, session_id: str | None, turn: Turn) -> None:
        """追加一轮对话，超出轮数上限则丢掉最早的。"""
        if not session_id:
            return
        with self._lock:
            turns = self._data.setdefault(session_id, [])
            turns.append(turn)
            if len(turns) > self.max_turns:
                del turns[: len(turns) - self.max_turns]
            self._data.move_to_end(session_id)
            while len(self._data) > self.max_sessions:
                self._data.popitem(last=False)

    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._data.clear()
            else:
                self._data.pop(session_id, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"sessions": len(self._data), "turns": sum(len(v) for v in self._data.values())}


REWRITE_PROMPT = """以下は越境EC店舗のカスタマーサポートでの会話です。
最後の買家の発言が、前の文脈なしでは意味が通じない場合があります。
最後の発言を、単独で検索しても意図が通じる**自己完結した質問文**に書き換えてください。

【これまでの会話】
{history}

【最後の発言】
{question}

ルール：
- 主語や対象（商品名・型番など）が省略されている場合は、会話から補ってください。
- すでに自己完結している場合は、原文をそのまま返してください。
- 出力は書き換えた質問文**だけ**。説明や引用符は付けないこと。"""


class QueryRewriter:
    """把多轮追问改写成自包含的检索语句。"""

    def __init__(self, llm: BaseChatModel, enabled: bool = True):
        self.llm = llm
        self.enabled = enabled

    def rewrite(self, question: str, history: list[Turn]) -> str:
        """返回用于检索的问题；无历史或失败时原样返回。"""
        if not self.enabled or not history:
            return question
        history_text = "\n".join(
            f"買家: {t.question}\nサポート: {t.answer[:120]}" for t in history
        )
        prompt = REWRITE_PROMPT.format(history=history_text[-1500:], question=question)
        try:
            with guarded("llm", health=True):
                resp = self.llm.invoke(prompt)
            rewritten = (getattr(resp, "content", "") or "").strip().strip('"').strip("「」")
            # 改写结果异常（空、过长、像一段解释）时保守回退
            if not rewritten or len(rewritten) > len(question) * 6 + 60:
                return question
            if rewritten != question:
                logger.info("查询改写: %r -> %r", question[:40], rewritten[:60])
            return rewritten
        except Exception as exc:  # noqa: BLE001 - 改写失败不影响主流程
            logger.warning("查询改写失败，使用原问题: %s", exc)
            return question
