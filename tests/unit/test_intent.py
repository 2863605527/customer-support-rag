"""意图模型单测：转人工判定必须「安全优先」。"""
from customer_support.models.intent import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    IntentCategory,
    IntentResult,
)


class TestIntentResult:
    def test_human_always_need_human(self):
        r = IntentResult(intent=IntentCategory.HUMAN, confidence=0.99)
        assert r.needs_human() is True
        assert r.is_high_risk is True

    def test_low_confidence_need_human(self):
        r = IntentResult(intent=IntentCategory.LOGISTICS, confidence=0.3)
        assert r.needs_human() is True
        assert r.is_high_risk is False

    def test_normal_intent_no_human(self):
        r = IntentResult(intent=IntentCategory.PRESALE, confidence=0.9)
        assert r.needs_human() is False

    def test_threshold_is_injectable(self):
        r = IntentResult(intent=IntentCategory.PRESALE, confidence=0.6)
        assert r.needs_human(0.5) is False
        assert r.needs_human(0.7) is True

    def test_boundary_is_not_below_threshold(self):
        r = IntentResult(intent=IntentCategory.AFTERSALE, confidence=DEFAULT_CONFIDENCE_THRESHOLD)
        assert r.needs_human() is False

    def test_backward_compatible_property(self):
        r = IntentResult(intent=IntentCategory.AFTERSALE, confidence=0.9)
        assert r.need_human is False
