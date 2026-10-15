"""本轮加固的回归测试：静态文件越界、入参长度、建单失败降级。

三个用例组各自对应一个真实缺陷（详见各项 docstring）。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from customer_support.api.app import _resolve_static
from customer_support.models.message import ChatMessage


# ======================================================================
# 1) SPA 回退的路径穿越（任意文件读取）
# ======================================================================
class TestStaticPathConfined:
    @pytest.fixture
    def dist(self, tmp_path):
        """构造 dist/ 与「库外敏感文件」，模拟真实目录布局。"""
        d = tmp_path / "dist"
        (d / "assets").mkdir(parents=True)
        (d / "index.html").write_text("<html>app</html>", encoding="utf-8")
        (d / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        # 库外的敏感文件：修复前 GET /%2e%2e/%2e%2e/.env 可直接下载它
        (tmp_path / ".env").write_text("LLM_API_KEY=sk-secret", encoding="utf-8")
        return d

    def test_normal_files_resolve(self, dist):
        assert _resolve_static(dist, "index.html") == (dist / "index.html").resolve()
        assert _resolve_static(dist, "assets/app.js") == (dist / "assets" / "app.js").resolve()

    @pytest.mark.parametrize(
        "evil",
        [
            "../.env",
            "../../.env",
            "assets/../../.env",
            "....//....//.env",
            "..\\..\\.env",  # Windows 分隔符同样不能穿出去
            "assets/../../../.env",
        ],
    )
    def test_traversal_returns_none(self, dist, evil):
        assert _resolve_static(dist, evil) is None

    def test_directory_and_missing_return_none(self, dist):
        assert _resolve_static(dist, "") is None
        assert _resolve_static(dist, ".") is None
        assert _resolve_static(dist, "nope.html") is None

    def test_symlink_escaping_dist_blocked(self, dist, tmp_path):
        """dist 内的软链接指向库外文件时也必须拦下（resolve() 会跟随链接）。"""
        link = dist / "leak.txt"
        try:
            link.symlink_to(tmp_path / ".env")
        except (OSError, NotImplementedError):
            pytest.skip("当前平台不允许创建符号链接")
        assert _resolve_static(dist, "leak.txt") is None


# ======================================================================
# 2) 请求入参长度上限（session_id 会作为 SQLite 主键与内存字典键）
# ======================================================================
class TestChatMessageLimits:
    def test_normal_message_accepted(self):
        msg = ChatMessage(
            question="いつ届きますか？", session_id="web-abc", buyer_id="b-1", language="ja"
        )
        assert msg.language == "ja"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"session_id": "x" * 129},
            {"buyer_id": "x" * 65},
            {"language": "j" * 9},
            {"question": ""},
            {"question": "q" * 2001},
        ],
    )
    def test_oversized_input_rejected(self, kwargs):
        with pytest.raises(ValidationError):
            ChatMessage(**kwargs)


# ======================================================================
# 3) 建单失败不能把异常穿透到买家
# ======================================================================
def _service_with_failing_tickets():
    """构造一个「意图=高风险 → 走转人工」的 ChatService，工单写入必定失败。"""
    from customer_support.agents.intent_agent import IntentAgent
    from customer_support.agents.qa_agent import QAAgent
    from customer_support.i18n.translator import Translator
    from customer_support.rag.generator import AnswerGenerator
    from customer_support.rag.retriever import Retriever
    from customer_support.services.chat_service import ChatService
    from customer_support.services.session import SessionStore
    from tests.conftest import FakeLLM, FakeRetrievalBackend

    class _BoomTicketService:
        def create(self, ticket):  # noqa: ANN001 - 测试替身
            raise RuntimeError("sqlite disk I/O error")

    llm = FakeLLM('{"intent": "human", "confidence": 0.9, "reason": "返金要求"}')
    retriever = Retriever(FakeRetrievalBackend(), top_k=4, score_threshold=0.35)
    return ChatService(
        intent_agent=IntentAgent(llm=llm, confidence_threshold=0.5),
        qa_agent=QAAgent(AnswerGenerator(retriever, llm=llm)),
        translator=Translator(llm=llm, default_language="ja"),
        default_language="ja",
        session_store=SessionStore(),
        ticket_service=_BoomTicketService(),
    )


class TestTicketFailureDoesNotBreakAnswer:
    """工单写库失败时仍要把话术交给买家。

    修复前 ``_transfer`` 直接调用 ``ticket_service.create``：异常会穿透
    ``handle()`` 的兜底（兜底本身也走 ``_transfer``，再抛一次），最终变成 500——
    直接违反「买家永远不会收到 500」这条承诺。
    """

    def test_handle_still_returns_transfer(self):
        result = _service_with_failing_tickets().handle(ChatMessage(question="返金しろ！"))

        assert result.need_human is True
        assert result.human_reason == "high_risk_intent"
        assert result.ticket_id is None, "建单失败时工单号为空，但回答不能为空"
        assert result.answer

    def test_stream_still_emits_done(self):
        events = list(_service_with_failing_tickets().handle_stream(ChatMessage(question="返金しろ！")))

        assert events[-1]["type"] == "done"
        assert events[-1]["result"].need_human is True
