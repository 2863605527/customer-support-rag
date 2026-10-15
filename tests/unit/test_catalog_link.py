"""商品链接识别单测：CatalogIndex 映射 + ChatService 检索词注入。

场景：买家直接贴一个货源链接问「什么材质 / 120 的床用多大尺寸」——
问题本身没有可检索的关键词，靠 URL 里的货源编号锁定具体商品。
（注意与 test_catalog.py 区分：那边测的是知识库文档构建，这边测的是检索增强。）
"""
import json

from customer_support.models.message import ChatMessage
from customer_support.rag.catalog import CatalogIndex
from tests.conftest import FakeRetrievalBackend
from tests.unit.test_stream import _chat_service, _hit

_PRODUCT = {
    "sku": "KB-0001",
    "goods_id": 11748832,
    "name_ja": "アルミ合金 蚊帳 ダブル（幅150cm）",
    "name_ja_literal": "刺繍防塵1.8m…蚊帳",
    "category_ja": "蚊帳・寝具アクセサリ",
    "materials": ["アルミ合金"],
    "bed_sizes": ["ダブル（幅150cm）"],
    "source_url": "https://detail.zhaojiafang.com/goods/11748832",
}


def _index(tmp_path, products=None) -> CatalogIndex:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "t",
                "count": len(products or [_PRODUCT]),
                "products": products or [_PRODUCT],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return CatalogIndex(path)


class TestCatalogIndex:
    def test_match_by_goods_url(self, tmp_path):
        index = _index(tmp_path)
        product = index.match("https://detail.zhaojiafang.com/goods/11748832 この商品の素材は何ですか？")
        assert product is not None and product.sku == "KB-0001"

    def test_match_by_goods_id_query_param(self, tmp_path):
        index = _index(tmp_path)
        product = index.match("https://example.com/detail?goods_id=11748832")
        assert product is not None and product.goods_id == 11748832

    def test_match_by_sku_code(self, tmp_path):
        index = _index(tmp_path)
        product = index.match("KB-0001の対応ベッドサイズを教えてください")
        assert product is not None and product.sku == "KB-0001"

    def test_no_match_without_link(self, tmp_path):
        index = _index(tmp_path)
        assert index.match("返品は可能ですか？") is None

    def test_missing_file_degrades_gracefully(self, tmp_path):
        index = CatalogIndex(tmp_path / "not-exist.json")
        assert index.available is False
        assert index.match("https://detail.zhaojiafang.com/goods/11748832") is None

    def test_augment_query_contains_product_name(self, tmp_path):
        index = _index(tmp_path)
        product = index.match("https://detail.zhaojiafang.com/goods/11748832")
        assert product is not None
        augmented = index.augment_query(product)
        assert "アルミ合金 蚊帳" in augmented and "KB-0001" in augmented


class TestChatServiceLinkAugmentation:
    def test_link_question_queries_with_product_name(self, tmp_path):
        """贴链接提问：检索词必须以商品名开头（锚定到该商品的 KB 文档）。"""
        backend = FakeRetrievalBackend([_hit()])
        service = _chat_service("アルミ合金製です。", catalog_index=_index(tmp_path))
        # 把 retriever 换成可观测查询的 backend
        service.qa_agent.generator.retriever.backend = backend

        question = (
            "https://detail.zhaojiafang.com/goods/11748832 この商品の素材は何ですか？"
            "幅120cmのベッドでも使えますか？"
        )
        list(service.handle_stream(ChatMessage(question=question)))

        assert backend.calls, "检索必须发生"
        query = backend.calls[0][0]
        assert query.startswith("アルミ合金 蚊帳"), f"检索词未注入商品名：{query}"

    def test_plain_question_unaffected(self, tmp_path):
        backend = FakeRetrievalBackend([_hit()])
        service = _chat_service("30日以内であれば返品可能です。", catalog_index=_index(tmp_path))
        service.qa_agent.generator.retriever.backend = backend

        list(service.handle_stream(ChatMessage(question="返品は可能ですか？")))

        assert backend.calls[0][0] == "返品は可能ですか？"

    def test_link_question_pins_product_document(self, tmp_path):
        """贴链接提问：即便相似度一无所获，该商品文档也必须被确定性锚定。

        回归真实故障——目标商品文档相似度只有 0.549、排不到第一，
        模型拿不到该商品资料就拒答转人工。
        """
        backend = FakeRetrievalBackend([])  # 相似度检索什么都给不出来
        backend.add_document("KB-0001.md", ["素材：綿100%", "サイズ：幅150cm"])
        service = _chat_service("綿100%です。", catalog_index=_index(tmp_path))
        service.qa_agent.generator.retriever.backend = backend

        events = list(
            service.handle_stream(
                ChatMessage(question="https://detail.zhaojiafang.com/goods/11748832 素材は何ですか？")
            )
        )
        done = events[-1]["result"]

        assert backend.pin_calls and backend.pin_calls[0][0] == "KB-0001.md"
        assert done.source == "knowledge" and done.need_human is False
        assert [r.source for r in done.references] == ["KB-0001.md", "KB-0001.md"]
        assert all(r.pinned for r in done.references)

    def test_plain_question_never_pins(self, tmp_path):
        backend = FakeRetrievalBackend([_hit()])
        service = _chat_service("30日以内であれば返品可能です。", catalog_index=_index(tmp_path))
        service.qa_agent.generator.retriever.backend = backend

        list(service.handle_stream(ChatMessage(question="返品は可能ですか？")))

        assert backend.pin_calls == [], "无商品链接时不应触发锚定"
