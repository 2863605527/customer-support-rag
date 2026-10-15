"""知识库加载器单测：递归扫描、过滤草稿、doc_id 可溯源。"""
from pathlib import Path

from customer_support.knowledge.loader import (
    iter_knowledge_files,
    load_knowledge_documents,
    to_langchain_documents,
)


class TestIterKnowledgeFiles:
    def test_recursive_and_filters_readme(self, tmp_knowledge_dir: Path):
        names = {p.name for p in iter_knowledge_files(tmp_knowledge_dir)}
        assert names == {"KB-0001.md", "after_sales_policy.md"}

    def test_skips_underscore_and_dot_files(self, tmp_knowledge_dir: Path):
        (tmp_knowledge_dir / "_draft.md").write_text("草稿", encoding="utf-8")
        (tmp_knowledge_dir / ".hidden.md").write_text("隐藏", encoding="utf-8")
        names = {p.name for p in iter_knowledge_files(tmp_knowledge_dir)}
        assert "_draft.md" not in names
        assert ".hidden.md" not in names

    def test_extension_filter(self, tmp_knowledge_dir: Path):
        (tmp_knowledge_dir / "note.json").write_text("{}", encoding="utf-8")
        exts = [".json"]
        names = {p.name for p in iter_knowledge_files(tmp_knowledge_dir, exts)}
        assert names == {"note.json"}

    def test_missing_dir_raises(self, tmp_path: Path):
        import pytest

        from customer_support.common.exceptions import KnowledgeError

        with pytest.raises(KnowledgeError):
            iter_knowledge_files(tmp_path / "nope")


class TestLoadKnowledgeDocuments:
    def test_doc_id_is_relative_path(self, tmp_knowledge_dir: Path):
        docs = load_knowledge_documents(tmp_knowledge_dir)
        ids = {d.doc_id for d in docs}
        assert "products/KB-0001.md" in ids
        assert "policies/after_sales_policy.md" in ids

    def test_to_langchain_documents_carries_metadata(self, tmp_knowledge_dir: Path):
        docs = load_knowledge_documents(tmp_knowledge_dir)
        lc = to_langchain_documents(docs)
        assert all("source" in d.metadata and "doc_id" in d.metadata for d in lc)
