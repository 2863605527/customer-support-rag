"""跨境定价模型：国内批发价（CNY）→ 日本站零售价（JPY，税込）。

这是个**显式可查**的定价规则，不是拍脑袋的随机数——真实店铺的定价本来就该有公式，
否则客服报出来的价格和后台对不上，工单会立刻炸。

成本构成：

1. **国内段成本**：货源批发价 + 打包/国内运费，按批发价的 ``DOMESTIC_COST_FACTOR`` 计；
2. **汇率**：``FX_CNY_JPY``（人民币 → 日元）。跨境店铺通常按季度锁汇，
   这里用固定值并集中在一处，改一处即可全库生效；
3. **跨境加价**：``CROSSBORDER_MARKUP`` 覆盖国际物流、平台佣金、推广费与退货损耗；
4. **消费税**：日本站售价含 10% 消费税，已并入加价系数；
5. **心理定价**：向上取整到最近的 ``ROUND_TO_JPY`` 日元，报价好念、不出现零头。

口径说明：本模块产出的是「参考上代（税込）」。货源成本（``data/raw`` 里的批发价）
是真实采集数据，零售价是基于上述规则的**派生值**——两者在文档里分开标注，不混为一谈。
"""
from __future__ import annotations

# —— 定价参数（改这里即可全库生效）——
FX_CNY_JPY: float = 20.5          # 人民币 → 日元汇率（锁汇口径）
DOMESTIC_COST_FACTOR: float = 1.15  # 国内段成本系数（打包 + 国内运费）
CROSSBORDER_MARKUP: float = 3.0     # 跨境加价系数（物流 + 佣金 + 退货损耗 + 消费税）
ROUND_TO_JPY: int = 100             # 取整粒度

# 日本站最低零售价：低于这个数说明货源价异常（例如配件单只、样品），
# 直接抬到地板价，避免客服报出「1 円」这种让买家以为是 bug 的价格。
MIN_RETAIL_JPY: int = 480


def wholesale_to_retail_jpy(wholesale_cny: float) -> int:
    """国内批发价（CNY）→ 日本站参考零售价（JPY，税込，向上取整）。"""
    if wholesale_cny <= 0:
        raise ValueError(f"批发价必须为正数: {wholesale_cny}")
    cost_cny = wholesale_cny * DOMESTIC_COST_FACTOR
    raw_jpy = cost_cny * FX_CNY_JPY * CROSSBORDER_MARKUP
    rounded = int(round(raw_jpy / ROUND_TO_JPY)) * ROUND_TO_JPY
    return max(rounded, MIN_RETAIL_JPY)


def cost_breakdown(wholesale_cny: float) -> dict[str, float]:
    """给出定价明细，便于在文档与排障时解释价格怎么来的。"""
    cost_cny = wholesale_cny * DOMESTIC_COST_FACTOR
    raw_jpy = cost_cny * FX_CNY_JPY * CROSSBORDER_MARKUP
    return {
        "wholesale_cny": round(wholesale_cny, 2),
        "domestic_cost_cny": round(cost_cny, 2),
        "pre_markup_jpy": round(raw_jpy / CROSSBORDER_MARKUP, 1),
        "retail_jpy": wholesale_to_retail_jpy(wholesale_cny),
    }
