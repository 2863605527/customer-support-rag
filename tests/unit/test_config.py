"""配置层单测。

重点回归：list 字段的环境变量必须是 **CSV** 写法（``a,b,c``），
不能被 pydantic-settings 的默认 JSON 解码拦下——
``RAGFLOW_DATASET_IDS=tiktok_bedding_kb`` 曾经直接抛 SettingsError。
"""
import pytest

from customer_support.config import (
    AgentSettings,
    KnowledgeSettings,
    RAGFlowSettings,
    Settings,
    _split_csv,
)


class TestSplitCsv:
    def test_single_value(self):
        assert _split_csv("tiktok_bedding_kb") == ["tiktok_bedding_kb"]

    def test_multiple_values_with_spaces(self):
        assert _split_csv("a, b ,c") == ["a", "b", "c"]

    def test_drops_empty_segments(self):
        assert _split_csv("a,,b,") == ["a", "b"]

    def test_non_string_passthrough(self):
        assert _split_csv(["x"]) == ["x"]
        assert _split_csv(None) is None


class TestEnvListParsing:
    def test_dataset_ids_single_name(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RAGFLOW_DATASET_IDS", "tiktok_bedding_kb")
        assert RAGFlowSettings().dataset_ids == ["tiktok_bedding_kb"]

    def test_dataset_ids_multiple(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RAGFLOW_DATASET_IDS", "kb_a,kb_b")
        assert RAGFlowSettings().dataset_ids == ["kb_a", "kb_b"]

    def test_supported_languages(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SUPPORTED_LANGUAGES", "ja,en,zh")
        assert AgentSettings().supported_languages == ["ja", "en", "zh"]

    def test_human_intents(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HUMAN_INTENTS", "human,complaint")
        assert AgentSettings().human_intents == ["human", "complaint"]

    def test_supported_extensions(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SUPPORTED_EXTENSIONS", ".md,.txt")
        assert KnowledgeSettings().supported_extensions == [".md", ".txt"]


class TestSettingsShape:
    def test_defaults_and_sections(self):
        s = Settings()
        assert s.retrieval.backend in {"ragflow", "milvus"}
        assert s.agent.default_language == "ja"
        assert 0 < s.retrieval.score_threshold < 1
        assert 0 < s.agent.confidence_threshold < 1
        assert s.knowledge.knowledge_base_dir.name == "knowledge_base"

    def test_ensure_dirs_creates_directories(self, tmp_path):
        from pathlib import Path

        s = Settings()
        s.output_dir = tmp_path / "output"
        s.log_dir = tmp_path / "logs"
        s.console.db_path = tmp_path / "output" / "console" / "console.db"
        s.knowledge.knowledge_base_dir = tmp_path / "kb"
        s.ensure_dirs()
        for d in (
            s.output_dir,
            s.log_dir,
            s.console.db_path.parent,
            Path(s.knowledge.knowledge_base_dir),
        ):
            assert Path(d).is_dir()

    def test_invalid_log_level_rejected(self):
        with pytest.raises(ValueError):
            Settings(log_level="LOUD")
