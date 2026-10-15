"""文档加载器：读取知识库目录下的 txt / md / pdf。

约定（写进文档，避免协作时的歧义）：
- 目录**递归**扫描，因此可以按业务分文件夹组织：``products/``、``policies/``；
- 以 ``_`` 或 ``.`` 开头的文件视为草稿/隐藏文件，跳过（例如 ``_draft.md``、
  ``README.md`` 之类不想进知识库的说明）；
- ``doc_id`` 用「相对知识库根目录的路径」，这样回答引用里出现的是
  ``products/quilt-001.md`` 这种一眼能定位到文件的信息，而不是一串 UUID。
"""
from __future__ import annotations

from pathlib import Path

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_core.documents import Document

from customer_support.common.exceptions import KnowledgeError
from customer_support.common.logging import get_logger
from customer_support.models.knowledge import KnowledgeDocument

logger = get_logger(__name__)

# 这些文件是给人看的说明，不是知识库内容
SKIP_STEMS = {"readme", "changelog", "license"}


def iter_knowledge_files(
    knowledge_dir: Path,
    extensions: list[str] | None = None,
) -> list[Path]:
    """列出目录下所有待入库文件（递归、确定性排序、过滤草稿与说明文件）。"""
    extensions = [e.lower() for e in (extensions or [".txt", ".md", ".pdf"])]
    if not knowledge_dir.is_dir():
        raise KnowledgeError(f"知识库目录不存在: {knowledge_dir}")

    files: list[Path] = []
    for path in sorted(knowledge_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        if path.name.startswith(("_", ".")):
            continue
        if path.stem.lower() in SKIP_STEMS:
            continue
        files.append(path)
    return files


def load_knowledge_documents(
    knowledge_dir: Path,
    extensions: list[str] | None = None,
) -> list[KnowledgeDocument]:
    """加载目录下所有支持格式的文档。"""
    docs: list[KnowledgeDocument] = []
    for f in iter_knowledge_files(knowledge_dir, extensions):
        try:
            docs.append(_load_one(f, knowledge_dir))
        except Exception as e:  # noqa: BLE001 - 单个文件坏掉不应中断整批入库
            logger.error("加载失败 %s: %s", f.name, e)
    logger.info("共加载 %d 个文档（目录: %s）", len(docs), knowledge_dir)
    return docs


def _load_one(path: Path, root: Path | None = None) -> KnowledgeDocument:
    """加载单个文档为标准模型。"""
    if path.suffix.lower() == ".pdf":
        raw_docs = PyPDFLoader(str(path)).load()
        content = "\n".join(d.page_content for d in raw_docs)
    else:
        content = TextLoader(str(path), encoding="utf-8").load()[0].page_content

    # doc_id 统一用 POSIX 风格的相对路径：它出现在 API 引用与日志里，
    # 跨平台（Windows 的 \ 与 Linux 的 /）保持一致才能稳定溯源自同一份文档
    doc_id = path.relative_to(root).as_posix() if root else path.name
    return KnowledgeDocument(source=path, doc_id=doc_id, content=content, title=path.stem)


def to_langchain_documents(docs: list[KnowledgeDocument]) -> list[Document]:
    """转成 langchain Document（供 splitter 使用），保留来源标识。"""
    return [
        Document(page_content=d.content, metadata={"source": d.doc_id or d.title, "doc_id": d.doc_id or d.title})
        for d in docs
    ]
