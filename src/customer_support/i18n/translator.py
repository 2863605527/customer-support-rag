"""多语言处理：语言识别 + 按语言组织回复。

设计取舍：
- **先本地判字形，再调 LLM**。日语（含假名）、韩语（谚文）、中文（纯汉字）这三种
  脚本本身就是强信号，用正则一次判断就够，不必为此付一次 LLM 调用。只有
  拉丁字母语言（英/西/法/德…）才交给 LLM 细分——这能把日本站最常见场景的
  首响延迟直接降下来。
- **失败一律回退店铺默认语言**（日本站=日语），而不是抛错：语言识别错顶多让回复
  语言不对，抛错则会让整通对话失败，两者代价不对称。
- 检索本身跨语言：知识库是日语文档，买家用中文提问也能命中（多语言向量模型），
  因此「翻译」只用于人工交接与排障，不进入主链路。
"""
from __future__ import annotations

import re

from langchain_core.language_models import BaseChatModel

from customer_support.common.logging import get_logger
from customer_support.common.resilience import guarded

logger = get_logger(__name__)

# 语言名/译名 → 语言代码
LANG_ALIASES: dict[str, str] = {
    "japanese": "ja", "日本語": "ja", "日語": "ja", "日语": "ja", "ja": "ja", "jp": "ja",
    "english": "en", "英語": "en", "英语": "en", "en": "en",
    "spanish": "es", "español": "es", "espanol": "es", "西班牙语": "es", "es": "es",
    "french": "fr", "français": "fr", "francais": "fr", "法语": "fr", "fr": "fr",
    "german": "de", "deutsch": "de", "德语": "de", "de": "de",
    "korean": "ko", "한국어": "ko", "韩语": "ko", "ko": "ko",
    "chinese": "zh", "中文": "zh", "汉语": "zh", "zh": "zh",
}

# 本地字形判断：假名 -> 日语；谚文 -> 韩语；汉字 -> 中文
_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


class Translator:
    """语言识别 + 翻译。"""

    def __init__(self, llm: BaseChatModel, default_language: str = "ja"):
        self.llm = llm
        self.default_language = default_language

    # ------------------------------------------------------------------
    # 语言识别
    # ------------------------------------------------------------------
    def detect_script(self, text: str) -> str | None:
        """基于字形快速判断 CJK 语言；无法判断返回 None。"""
        if _KANA_RE.search(text):
            return "ja"
        if _HANGUL_RE.search(text):
            return "ko"
        # 纯汉字且无假名 -> 中文（日语句子几乎必然含假名）
        if _HAN_RE.search(text) and not _LATIN_RE.search(text):
            return "zh"
        return None

    def detect(self, text: str) -> str:
        """识别语言，返回语言代码；失败回退默认语言（日语）。"""
        text = (text or "").strip()
        if not text:
            return self.default_language

        script_guess = self.detect_script(text)
        if script_guess:
            logger.debug("字形识别语言: %s", script_guess)
            return script_guess

        # 拉丁字母语言交给 LLM 细分
        prompt = (
            "Identify the language of the text below. "
            "Reply with ONLY one of these codes: en, es, fr, de. "
            "No other words.\n\n"
            f"{text[:200]}"
        )
        try:
            with guarded("llm", health=True):
                resp = self.llm.invoke(prompt)
            raw = (getattr(resp, "content", "") or "").strip()
            token = re.split(r"[\s.,;:!?]+", raw)[0].lower() if raw else ""
            code = LANG_ALIASES.get(token)
            if code:
                logger.debug("LLM 识别语言: %s", code)
                return code
            logger.info("语言识别结果无法映射，回退默认: %r", raw[:40])
        except Exception as exc:  # noqa: BLE001 - 识别失败不阻断主流程
            logger.warning("语言识别调用失败，回退默认 %s: %s", self.default_language, exc)
        return self.default_language

    # ------------------------------------------------------------------
    # 翻译（人工交接 / 排障用，不在主链路）
    # ------------------------------------------------------------------
    def translate(self, text: str, target_lang: str | None = None) -> str:
        """把内容翻译到目标语言（默认日语，日本站客服交接用）。"""
        target = target_lang or self.default_language
        prompt = (
            f"Translate the following into {target}. "
            f"Output the translation only, no explanations.\n\n{text[:800]}"
        )
        resp = self.llm.invoke(prompt)
        return (getattr(resp, "content", "") or "").strip()
