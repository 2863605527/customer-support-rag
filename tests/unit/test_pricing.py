"""跨境定价模型单测。

定价是「客服报出来的价格」与「后台对不上就炸工单」的高风险点，必须有回归测试。
"""
import pytest

from customer_support.knowledge import pricing


class TestWholesaleToRetail:
    def test_known_example_matches_generated_doc(self):
        # KB-0001 蚊帐 批发 240 元 → 17,000 円（与生成文档中的报价一致）
        assert pricing.wholesale_to_retail_jpy(240) == 17000

    def test_rounding_to_nearest_hundred(self):
        # 10 元 → 10*1.15*20.5*3 = 707.25 → 700
        assert pricing.wholesale_to_retail_jpy(10) == 700

    def test_floor_applied_for_tiny_price(self):
        assert pricing.wholesale_to_retail_jpy(0.5) == pricing.MIN_RETAIL_JPY

    def test_monotonic(self):
        prices = [pricing.wholesale_to_retail_jpy(x) for x in (5, 50, 500, 5000)]
        assert prices == sorted(prices)

    def test_non_positive_raises(self):
        for bad in (0, -1):
            with pytest.raises(ValueError):
                pricing.wholesale_to_retail_jpy(bad)


class TestCostBreakdown:
    def test_keys_and_consistency(self):
        b = pricing.cost_breakdown(240)
        assert set(b) == {"wholesale_cny", "domestic_cost_cny", "pre_markup_jpy", "retail_jpy"}
        assert b["retail_jpy"] == pricing.wholesale_to_retail_jpy(240)
        # 国内段成本 = 批发价 * 系数
        assert b["domestic_cost_cny"] == pytest.approx(240 * pricing.DOMESTIC_COST_FACTOR, rel=1e-6)
