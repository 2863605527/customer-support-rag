"""意图分类模型。"""
from enum import Enum

from pydantic import BaseModel, Field


class IntentCategory(str, Enum):
    """意图类别（稳定枚举，prompt 与代码共用）。"""

    PRESALE = "presale"            # 售前咨询
    AFTERSALE = "aftersale"        # 售后问题
    LOGISTICS = "logistics"        # 物流问题
    PRODUCT_INFO = "product_info"  # 商品信息
    HUMAN = "human"                # 必须转人工


# 默认置信度阈值：低于此值一律转人工（安全优先）
DEFAULT_CONFIDENCE_THRESHOLD = 0.5


class IntentResult(BaseModel):
    """意图识别输出。"""

    intent: IntentCategory
    confidence: float = Field(0.0, ge=0, le=1)
    reason: str = Field("", description="分类理由（调试用）")
    fallback: bool = Field(
        False,
        description="是否因识别失败而兜底产生（不是模型判定的结果，仅用于保守转人工）",
    )

    def needs_human(self, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> bool:
        """高风险类别或置信度低于阈值 -> 转人工。"""
        return self.intent == IntentCategory.HUMAN or self.confidence < threshold

    @property
    def need_human(self) -> bool:
        """按默认阈值判断（保持向后兼容）。"""
        return self.needs_human()

    @property
    def is_high_risk(self) -> bool:
        """是否命中高风险类别（用于区分转人工原因）。

        ``fallback=True`` 必须排除在外：识别失败时我们也返回 ``HUMAN``
        （方向保守，宁可不作答也不乱答），但那是「没能识别出意图」，
        而不是「买家在高风险诉求」。

        不排除会怎样——实测踩过：DeepSeek 超时/503 时意图识别兜底成 ``human``，
        于是工单归因写成 ``high_risk_intent``（工单标题：高风险诉求）。
        运营看到的成了「买家投诉激增」，而真正的原因（依赖故障）被掩盖，
        专门为此定义的 ``service_unavailable`` 反而永远不出现。
        """
        return self.intent == IntentCategory.HUMAN and not self.fallback
