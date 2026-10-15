"""转人工归因单测：**依赖故障 ≠ 买家高风险诉求**。

背景（启动项目时实测踩到，不是纸上推演）：

DeepSeek 高峰期返回 ``503 Service is too busy`` 或直接超时。此时意图识别拿不到结果，
Agent 层兜底成 ``intent=human``；ChatService 按 ``is_high_risk``（等价于
``intent == HUMAN``）归因，于是工单上写的是 ``high_risk_intent``——工单标题是
「高风险诉求」。真实日志：

    意图解析失败(第 2 次): Error code: 503 - Service is too busy...
    意图解析连续失败，兜底转人工: 敷きパッドは洗濯機で洗えますか？
    转人工 reason=high_risk_intent intent=human confidence=0.00

一个问「床垫能机洗吗」的正常买家，被记成了高风险投诉。运营复盘时会把
「上游抖动」误读成「投诉激增」，而专门为依赖故障定义的 ``service_unavailable``
反而永远不会出现。本文件把这四条归因路径全部钉住。
"""
from __future__ import annotations

import pytest

from customer_support.agents.intent_agent import IntentAgent
from customer_support.agents.qa_agent import QAAgent
from customer_support.common.resilience import (
    CircuitOpenError,
    classify_llm_error,
    is_dependency_unavailable,
    reset_resilience,
)
from customer_support.i18n.translator import Translator
from customer_support.models.intent import IntentCategory, IntentResult
from customer_support.models.message import ChatMessage
from customer_support.rag.generator import AnswerGenerator
from customer_support.rag.retriever import Retriever
from customer_support.services.chat_service import ChatService
from customer_support.services.session import SessionStore
from tests.conftest import FakeLLM, FakeRetrievalBackend


class _FailingLLM:
    """``invoke`` 一律抛指定异常，用于模拟上游 LLM 故障。"""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    def invoke(self, prompt: str):
        self.calls += 1
        raise self.exc


class _StatusError(Exception):
    """模拟 openai.APIStatusError：状态码挂在异常自身的 status_code 上。"""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _ResponseError(Exception):
    """模拟 httpx 风格：状态码挂在 exc.response.status_code 上。"""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.response = type("_Resp", (), {"status_code": status_code})()


@pytest.fixture(autouse=True)
def _clean_resilience():
    """熔断器是进程内全局状态，必须逐用例复位（否则前一个用例的失败会串味）。"""
    reset_resilience()
    yield
    reset_resilience()


def _service(intent_llm) -> ChatService:
    retriever = Retriever(FakeRetrievalBackend([]), top_k=4, score_threshold=0.35)
    return ChatService(
        intent_agent=IntentAgent(llm=intent_llm, confidence_threshold=0.5),
        qa_agent=QAAgent(AnswerGenerator(retriever, llm=FakeLLM(""))),
        translator=Translator(llm=FakeLLM("ja"), default_language="ja"),
        default_language="ja",
        session_store=SessionStore(),
    )


def _ask(service: ChatService, question: str = "敷きパッドは洗濯機で洗えますか？"):
    # 显式给 language 以跳过语言识别，让用例只考察归因逻辑
    return service.handle(ChatMessage(question=question, language="ja"))


class TestIsDependencyUnavailable:
    @pytest.mark.parametrize(
        "exc",
        [
            CircuitOpenError("llm"),
            _StatusError("upstream boom", 503),
            _StatusError("upstream boom", 502),
            _ResponseError("upstream boom", 500),
            RuntimeError("Request timed out"),
            RuntimeError("Error code: 503 - Service is too busy"),
            RuntimeError("429 Too Many Requests"),
            RuntimeError("Connection error"),
            RuntimeError("402 Insufficient Balance"),
        ],
    )
    def test_dependency_failures(self, exc):
        assert is_dependency_unavailable(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("something weird"),
            ValueError("unexpected type"),
            KeyError("missing"),
            _StatusError("bad request", 400),
        ],
    )
    def test_not_dependency_failures(self, exc):
        assert is_dependency_unavailable(exc) is False

    def test_503_is_classified_as_unavailable(self):
        """原先 503 落进 unknown，/readyz 的 last_error_category 看不出问题在哪。"""
        assert classify_llm_error(RuntimeError("Error code: 503 - Service is too busy")) == (
            "unavailable"
        )


class TestFallbackIsNotHighRisk:
    """兜底产生的 HUMAN 不能算「高风险诉求」。"""

    def test_explicit_human_is_high_risk(self):
        assert IntentResult(intent=IntentCategory.HUMAN, confidence=0.9).is_high_risk is True

    def test_fallback_human_is_not_high_risk(self):
        result = IntentResult(
            intent=IntentCategory.HUMAN, confidence=0.0, reason="intent_parse_failed", fallback=True
        )
        assert result.is_high_risk is False
        # 方向不变：仍然转人工，只是换一个诚实的归因
        assert result.needs_human(0.5) is True


class TestTransferAttribution:
    """四条归因路径端到端钉住。"""

    def test_model_says_human_stays_high_risk(self):
        """模型自己判定 human：仍是 high_risk_intent（原有行为不能改坏）。"""
        llm = FakeLLM('{"intent": "human", "confidence": 0.9, "reason": "返金要求"}')
        result = _ask(_service(llm), question="返金しろ！")

        assert result.need_human is True
        assert result.human_reason == "high_risk_intent"

    def test_llm_timeout_is_service_unavailable(self):
        """上游超时 = 依赖不可用，不是买家高风险。"""
        result = _ask(_service(_FailingLLM(RuntimeError("Request timed out"))))

        assert result.need_human is True
        assert result.human_reason == "service_unavailable", (
            "LLM 超时被归因成高风险会掩盖真实故障"
        )

    def test_llm_503_is_service_unavailable(self):
        """上游 503（实测 DeepSeek 高峰期原样返回）。"""
        exc = _StatusError("503 Service is too busy", 503)
        result = _ask(_service(_FailingLLM(exc)))

        assert result.human_reason == "service_unavailable"

    def test_code_error_is_system_error(self):
        """无法识别的异常 = 可能是我们自己的 bug，才归 system_error。"""
        result = _ask(_service(_FailingLLM(ValueError("unexpected shape"))))

        assert result.human_reason == "system_error"

    def test_unparseable_output_is_low_confidence(self):
        """模型两次都吐不出合法 JSON：兜底转人工，但归因是低置信度而非高风险。"""
        result = _ask(_service(FakeLLM("这不是 JSON，只是普通文本")))

        assert result.need_human is True
        assert result.human_reason == "low_confidence"

    def test_stream_attribution_matches_handle(self):
        """流式链路与同步链路归因必须一致（两条路径各自 catch，容易漏改一条）。"""
        events = list(
            _service(_FailingLLM(RuntimeError("Request timed out"))).handle_stream(
                ChatMessage(question="敷きパッドは洗濯機で洗えますか？", language="ja")
            )
        )
        done = events[-1]["result"]

        assert done.need_human is True
        assert done.human_reason == "service_unavailable"
