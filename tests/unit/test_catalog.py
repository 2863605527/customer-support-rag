"""商品目录构建与渲染单测（货源记录 → 日语知识库文档）。"""
from customer_support.knowledge.catalog import (
    CATEGORY_JA,
    CATEGORY_PITCH,
    build_catalog,
    build_product,
    material_value_ja,
    render_product_md,
    season_value_ja,
)


def _record(**over):
    base = {
        "goods_id": 11748832,
        "goods_name": "新款铝合金可升降导轨蚊帐 1.5m（5英尺）床 冰花",
        "category": "accessory",
        "price_cny_wholesale": 240.0,
        "store_name": "海虹蚊帐",
        "clicks": 7250,
        "stock": 120,
        "image_url": "https://img.example/a.jpg",
        "detail_url": "https://m.zhaojiafang.com/goods/11748832",
        "tags": [{"tagname": "高密度"}, {"tagname": "防蚊"}],
    }
    base.update(over)
    return base


class TestBuildProduct:
    def test_fields_mapped(self):
        p = build_product(_record(), "KB-0001")
        assert p.sku == "KB-0001"
        assert p.goods_id == 11748832
        assert p.category_ja == CATEGORY_JA["accessory"]
        assert p.price_jpy == 17000
        assert p.wholesale_cny == 240.0
        assert p.supplier == "海虹蚊帐"
        assert p.popularity == 7250
        assert p.pattern == "冰花"
        assert "ダブル（幅150cm）" in p.bed_sizes
        assert "アルミ合金" in p.materials
        assert p.pitch  # 蚊帳/アルミ 命中素材卖点

    def test_pitch_falls_back_to_category(self):
        # 无素材命中的记录：卖点应落到品类兜底，而不是通用文案
        p = build_product(
            _record(goods_name="神秘商品", category="quilt"), "KB-0002"
        )
        assert p.pitch == CATEGORY_PITCH["quilt"]


class TestBuildCatalog:
    def test_sku_numbering_and_grouping(self):
        records = [
            _record(goods_id=1, category="quilt", clicks=10),
            _record(goods_id=2, category="accessory", clicks=99),
            _record(goods_id=3, category="quilt", clicks=50),
        ]
        catalog = build_catalog(records)
        assert [p.sku for p in catalog] == ["KB-0001", "KB-0002", "KB-0003"]
        # accessory 排在 quilt 前（按 category 字符串排序）
        assert catalog[0].category == "accessory"
        # 同品类内按点击量降序
        assert catalog[1].goods_id == 3
        assert catalog[2].goods_id == 1

    def test_existing_skus_reused_and_new_ones_appended(self):
        records = [
            _record(goods_id=1, category="accessory", clicks=50),
            _record(goods_id=2, category="quilt", clicks=90),
            _record(goods_id=3, category="quilt", clicks=10),
        ]
        catalog = build_catalog(records, existing_skus={2: "KB-0007", 3: "KB-0008"})
        by_id = {p.goods_id: p.sku for p in catalog}
        # 已有番号原样沿用
        assert by_id[2] == "KB-0007"
        assert by_id[3] == "KB-0008"
        # 新商品接在最大番号之后，且不与人冲突
        assert by_id[1] == "KB-0009"
        assert len({p.sku for p in catalog}) == 3

    def test_expanding_selection_keeps_old_numbers(self):
        # 模拟「60 → 120」扩品：老 SKU 番号必须一个都不变
        old_records = [
            _record(goods_id=10, category="quilt", clicks=500),
            _record(goods_id=11, category="quilt", clicks=400),
        ]
        old_catalog = build_catalog(old_records)
        old_map = {p.goods_id: p.sku for p in old_catalog}
        assert old_map == {10: "KB-0001", 11: "KB-0002"}

        # 扩品后新增一条点击量更高的商品，插在品类最前
        expanded = old_records + [_record(goods_id=12, category="quilt", clicks=900)]
        new_catalog = build_catalog(expanded, existing_skus=old_map)
        new_map = {p.goods_id: p.sku for p in new_catalog}
        assert new_map[10] == "KB-0001"
        assert new_map[11] == "KB-0002"
        assert new_map[12] == "KB-0003"
        # 排序确实变了（12 在最前），但番号没跟着挪
        assert [p.goods_id for p in new_catalog] == [12, 10, 11]

    def test_non_standard_sku_does_not_break_numbering(self):
        records = [_record(goods_id=1, category="quilt", clicks=1)]
        catalog = build_catalog(records, existing_skus={99: "legacy-sku"})
        assert catalog[0].sku == "KB-0001"


class TestMaterialValueJa:
    def test_combined_value_split_into_parts(self):
        assert material_value_ja("聚酯纤维+10%大豆纤维") == "ポリエステル＋大豆ファイバー10%"

    def test_alias_in_parentheses_resolved(self):
        assert material_value_ja("涤纶(聚酯纤维)") == "ポリエステル"
        assert material_value_ja("氨纶（聚氨酯弹性纤维）") == "ポリウレタン"

    def test_noise_value_returns_empty(self):
        for value in ("其他", "other", "其他/other", "无", "请选择", "", "   "):
            assert material_value_ja(value) == "", value

    def test_unknown_value_kept_verbatim(self):
        assert material_value_ja("某某新材料") == "某某新材料"

    def test_render_drops_noise_attrs(self):
        p = build_product(_record(), "KB-0001")
        p.material_attrs = {"材质": "其他", "面料": "棉"}
        md = render_product_md(p)
        assert "生地=綿（コットン）" in md
        assert "素材=" not in md  # 「其他」不应出现


class TestSeasonValueJa:
    def test_common_values_translated(self):
        assert season_value_ja("四季通用") == "オールシーズン"
        assert season_value_ja("春秋") == "春・秋"
        assert season_value_ja("冬季") == "冬"

    def test_unknown_value_kept(self):
        assert season_value_ja("未知季节") == "未知季节"
        assert season_value_ja("") == ""


class TestRenderProductMd:
    def test_sections_present(self):
        md = render_product_md(build_product(_record(), "KB-0001"))
        for heading in (
            "# ",
            "## 価格",
            "## サイズ",
            "## 素材・特徴",
            "## お手入れ方法",
            "## ご注意",
            "## 検索キーワード（社内用）",
            "## 社内参考情報",
        ):
            assert heading in md
        assert "KB-0001" in md
        assert "17,000円" in md
        # 社内参考段要能看到原始中文名与成本，便于客服核对
        assert "海虹蚊帐" in md
        assert "240.00元" in md

    def test_set_contents_rendered_for_4piece(self):
        md = render_product_md(
            build_product(_record(goods_name="全棉双层纱四件套 200*230cm"), "KB-0009")
        )
        assert "## セット内容" in md
        assert "4点セット" in md
