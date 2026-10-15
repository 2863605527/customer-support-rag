"""中文货源名 → 日语结构化属性的单测。

这些断言直接对应「客服照着念会不会被买家追问」的真实风险：
标题里不能出现中文原词、尺寸/件数必须准确、保养建议要与素材匹配。
"""
from customer_support.knowledge import localization as L


class TestConsumeTerms:
    def test_longest_match_consumed_once(self):
        """全棉双层纱 应整体命中，不会被 全棉 / 双层纱 重复翻译。"""
        found, leftover = L.consume_terms("全棉双层纱被套", L.MATERIAL_TERMS)
        assert "綿100%二重ガーゼ" in found
        assert "綿100%" not in found
        assert "全棉" not in leftover
        assert "双层纱" not in leftover

    def test_duplicate_term_consumed_once(self):
        found, _ = L.consume_terms("纯棉纯棉", L.MATERIAL_TERMS)
        assert found.count("綿100%") == 1


class TestDetect:
    def test_detect_materials_and_categories(self):
        name = "全棉双层纱四件套"
        assert L.detect_materials(name) == ["綿100%二重ガーゼ"]
        assert L.detect_categories(name) == ["4点セット"]

    def test_strip_noise_removes_marketing_words(self):
        out = L.strip_noise("新款包邮 全棉被套 2024")
        assert "新款" not in out
        assert "包邮" not in out
        assert "2024" not in out
        assert "全棉" in out


class TestSizes:
    def test_cm_size(self):
        cms, _ = L.extract_sizes("被套 200*230cm")
        assert cms == ["200×230cm"]

    def test_three_dimension_size(self):
        cms, _ = L.extract_sizes("蚊帐 150x200x170cm")
        assert cms == ["150×200×170cm"]

    def test_bed_width_from_meter(self):
        _, widths = L.extract_sizes("蚊帐 1.5m（5英尺）床 冰花")
        assert "ダブル（幅150cm）" in widths

    def test_specs_and_pieces(self):
        assert L.extract_specs("四件套床笠款") == ["ボックスシーツ仕様"]
        assert L.extract_set_pieces("四件套") == 4
        assert L.extract_set_pieces("三件套") == 3
        assert L.extract_set_pieces("被套单件") is None


class TestPattern:
    def test_pattern_extracted_after_consuming_terms(self):
        title = "新款绣花被套 冰花"
        assert L.extract_pattern(title) == "冰花"

    def test_pattern_splits_on_hyphen(self):
        # 碧晨春泥-强遮光：「强遮光」是功能说明不是花型名，应被拆分掉
        assert L.extract_pattern("碧晨春泥-强遮光") == "碧晨春泥"

    def test_pattern_empty_when_all_consumed(self):
        assert L.extract_pattern("全棉被套") == ""


class TestComposeTitle:
    def test_title_uses_only_japanese_attributes(self):
        title = L.compose_title("全棉双层纱四件套 200*230cm")
        assert title == "綿100%二重ガーゼ 4点セット 200×230cm"
        # 面向买家的标题里不允许出现中文原词（注意：日文汉字也在 CJK 区，
        # 因此只能按「具体中文词」断言，不能用统一表意文字范围判断）
        for cn_word in ("全棉", "双层纱", "四件套", "纯棉"):
            assert cn_word not in title

    def test_title_falls_back_to_bedding(self):
        assert L.compose_title("神秘商品") == "寝具"


class TestCareAndPitch:
    def test_care_by_material(self):
        assert L.care_instructions("綿100%") == L.CARE_BY_MATERIAL["綿100%"]

    def test_care_default_when_unknown(self):
        assert "洗濯表示" in L.care_instructions("未知素材")[0]

    def test_pitch_hit_and_miss(self):
        assert L.material_pitch("アルミ合金フレームの蚊帳") == L.PITCH_BY_KEYWORD[0][1]
        # 没命中时返回空串，交给品类级兜底
        assert L.material_pitch("まったく未知") == ""


class TestLocalizeName:
    def test_keeps_residual_for_keyword_search(self):
        out = L.localize_name("新款全棉被套 冰花")
        assert "綿100%" in out
        assert "掛け布団カバー" in out
        assert "新款" not in out
        assert "冰花" in out
