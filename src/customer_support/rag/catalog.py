"""商品目录索引：把买家消息里的货源链接映射成具体商品。

买家经常直接贴一个商品链接再问「什么材质 / 120 的床用多大尺寸」——
这种问题的关键词（"これ"/"この商品"）本身没有检索价值，能命中正确商品
文档的锚点只有 URL 里的货源编号。``CatalogIndex`` 负责这一步映射：

1. 用正则从消息里提取 URL；
2. 从 URL 提取货源编号（goods id），到 ``catalog.json``（数据流水线产物，
   60 SKU，含 ``source_url`` / ``goods_id`` / 日文品名）里查商品；
3. 上层（ChatService）把日文品名注入检索词，让检索直接命中该商品的
   KB 文档——这比指望向量检索"理解"一个 URL 可靠得多。

找不到目录文件时优雅降级为「无索引」（``available=False``），不影响主链路。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from customer_support.common.logging import get_logger

logger = get_logger(__name__)

# 从自由文本中提取 URL（客服消息里链接通常独立出现或跟在标点前）
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
# 货源编号：/goods/12345678 或 goods_id=12345678（query 参数形式兜底）
_GOODS_ID_RE = re.compile(r"(?:/goods/|[?&]goods_id=)(\d+)", re.IGNORECASE)
# 商品番号（如 KB-0033）。不能用 \b：日文假名/汉字在 Unicode 正则里也算 word 字符，
# "KB-0001のサイズ" 会因此失去词边界而匹配不到。这里用显式前后断言。
_SKU_RE = re.compile(r"(?<![A-Za-z0-9])KB-(\d{4})(?!\d)", re.IGNORECASE)


class CatalogProduct(BaseModel):
    """catalog.json 里的一条商品（只保留检索增强需要的字段）。"""

    sku: str
    goods_id: int
    name_ja: str
    name_ja_literal: str = ""
    category_ja: str = ""
    pattern: str = ""
    materials: list[str] = Field(default_factory=list)
    bed_sizes: list[str] = Field(default_factory=list)
    source_url: str = ""

    @property
    def doc_name(self) -> str:
        """该商品对应的知识库文档名。

        入库时用的是本地文件名，商品文档命名规则固定为 ``{sku}.md``，
        因此由 sku 即可推出 RAGFlow 侧的文档名（锚定检索的依据）。
        """
        return f"{self.sku}.md"


class CatalogIndex:
    """货源链接 → 商品 的只读索引（进程内一份，懒加载）。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._by_goods_id: dict[int, CatalogProduct] = {}
        self._by_sku: dict[str, CatalogProduct] = {}
        self.available = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            logger.warning("商品目录不存在，链接识别不可用：%s", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("商品目录解析失败，链接识别不可用：%s（%s）", self.path, exc)
            return

        products = data.get("products") if isinstance(data, dict) else data
        if not isinstance(products, list):
            logger.warning("商品目录格式不符合预期（缺少 products 数组）：%s", self.path)
            return

        for raw in products:
            try:
                product = CatalogProduct.model_validate(raw)
            except Exception:  # noqa: BLE001 - 单条脏数据不应拖垮整个目录
                continue
            self._by_goods_id[product.goods_id] = product
            self._by_sku[product.sku.upper()] = product
        self.available = bool(self._by_goods_id)
        logger.info("商品目录加载完成：%d 条（%s）", len(self._by_goods_id), self.path.name)

    def match(self, text: str) -> CatalogProduct | None:
        """从消息文本里识别商品：优先货源链接，其次直接写明的商品编号（KB-XXXX）。"""
        if not self.available:
            return None
        for url in _URL_RE.findall(text):
            m = _GOODS_ID_RE.search(url)
            if m and int(m.group(1)) in self._by_goods_id:
                return self._by_goods_id[int(m.group(1))]
        # 兜底：买家/客服直接引用商品番号（如 KB-0033）
        for m in _SKU_RE.finditer(text):
            sku = f"KB-{m.group(1)}".upper()
            if sku in self._by_sku:
                return self._by_sku[sku]
        return None

    def augment_query(self, product: CatalogProduct) -> str:
        """生成检索增强词：完整日文标题（含花型）为主，SKU 兜底。

        花型（pattern）往往是同名系列里唯一区分商品的部分（如「冰花」），
        带上它能让 RAGFlow 的混合检索把该商品排到第一；中文名反而不放——
        实测会稀释向量、把中文长列表分块顶上来。
        """
        parts = [product.name_ja]
        if product.pattern:
            parts.append(product.pattern)
        parts.append(product.sku)
        return " ".join(parts)
