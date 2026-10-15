"""多语言识别单测：本地字形优先，Latin 才走 LLM，失败回退日语。"""
from customer_support.i18n.translator import Translator
from tests.conftest import FakeLLM


class TestDetectScript:
    def setup_method(self):
        self.t = Translator(FakeLLM("en"))

    def test_japanese_kana(self):
        assert self.t.detect_script("商品はいつ届きますか？") == "ja"

    def test_korean_hangul(self):
        assert self.t.detect_script("상품은 언제 도착하나요?") == "ko"

    def test_chinese_han_only(self):
        assert self.t.detect_script("这个被子多少钱") == "zh"

    def test_latin_is_unknown_at_script_level(self):
        assert self.t.detect_script("When will it arrive?") is None


class TestDetect:
    def test_script_hit_does_not_call_llm(self):
        llm = FakeLLM("en")
        t = Translator(llm)
        assert t.detect("商品はいつ届きますか？") == "ja"
        assert llm.calls == []  # 日语不走 LLM

    def test_latin_uses_llm(self):
        t = Translator(FakeLLM("en"))
        assert t.detect("When will it arrive?") == "en"

    def test_llm_garbage_falls_back_to_default(self):
        t = Translator(FakeLLM("我不知道"), default_language="ja")
        assert t.detect("When will it arrive?") == "ja"

    def test_llm_error_falls_back_to_default(self):
        class Boom:
            def invoke(self, prompt):
                raise RuntimeError("timeout")

        t = Translator(Boom(), default_language="ja")
        assert t.detect("Bonjour, quand?") == "ja"

    def test_empty_returns_default(self):
        t = Translator(FakeLLM("en"))
        assert t.detect("") == "ja"


class TestTranslate:
    def test_returns_llm_content(self):
        t = Translator(FakeLLM("ご質問ありがとうございます。"))
        assert t.translate("谢谢") == "ご質問ありがとうございます。"
