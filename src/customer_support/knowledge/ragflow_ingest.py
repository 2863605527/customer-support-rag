"""把本地知识库文档灌入 RAGFlow（离线侧，与在线问答解耦）。

为什么入库逻辑要单独成一个模块、而不是塞进 CLI：

- **可重复执行**：知识库文档会不断更新，入库必须是幂等的——同名文档默认跳过，
  ``--replace`` 才覆盖，避免每次更新都把整库重灌一遍（跨境店铺文档几百份，
  重灌一次要等 deepdoc 跑几分钟）。
- **可审计**：执行结果返回结构化的 ``IngestReport``（新增/跳过/删除/失败各多少），
  CI 里可以直接断言「期望 N 份文档全部 DONE」。
- **不静默删数据**：清理远端多余文档时，先把远端正文备份到
  ``data/raw/ragflow_backup/``，再删除——运维脚本最忌讳的就是「悄悄删掉别人的数据」。

解析是异步的：``POST /datasets/{id}/chunks`` 只是触发，状态机是
``UNSTART -> RUNNING -> DONE / FAIL / CANCEL``，因此必须轮询等待。
"""
from __future__ import annotations

import json
import mimetypes
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from customer_support.common.exceptions import ExternalAPIError, KnowledgeError
from customer_support.common.logging import get_logger
from customer_support.config import Settings

logger = get_logger(__name__)

TERMINAL_STATES = {"DONE", "FAIL", "CANCEL"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# RAGFlow 业务码：文档已不在 UNSTART（通常因为上传后自动开始解析）
ALREADY_PARSING_CODE = 102


@dataclass
class IngestReport:
    """一次入库执行的结果。"""

    dataset_id: str = ""
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    parse_status: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        done = [d for d, s in self.parse_status.items() if s == "DONE"]
        return not self.failed and len(done) == len(self.parse_status)

    def to_dict(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "uploaded": self.uploaded,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "failed": self.failed,
            "parse_status": self.parse_status,
            "ok": self.ok,
        }


class RagflowIngestor:
    """RAGFlow 知识库入库客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ):
        if not api_key:
            raise KnowledgeError("未配置 RAGFLOW_API_KEY，无法把文档灌入 RAGFlow")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        return self._client

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> Any:
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("RAGFlow %s %s 异常(%d/%d): %s", method, path, attempt, self.max_retries, last_error)
                if attempt == self.max_retries:
                    break
                time.sleep(min(2.0 * attempt, 6.0))
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                last_error = f"HTTP {resp.status_code}"
                logger.warning("RAGFlow %s 返回 %s，重试(%d/%d)", path, last_error, attempt, self.max_retries)
                time.sleep(min(2.0 * attempt, 6.0))
                continue

            try:
                payload = resp.json()
            except ValueError:
                raise ExternalAPIError(
                    "RAGFlow 返回非 JSON 响应",
                    detail=f"HTTP {resp.status_code}: {resp.text[:200]}",
                ) from None

            if resp.status_code >= 400:
                raise ExternalAPIError(
                    f"RAGFlow 请求失败 HTTP {resp.status_code} ({path})",
                    detail=str(payload)[:300],
                )
            code = payload.get("code") if isinstance(payload, dict) else None
            if code not in (0, None):
                raise ExternalAPIError(
                    f"RAGFlow 业务错误 code={code} ({path})",
                    detail=str(payload.get("message"))[:300],
                    remote_code=int(code) if isinstance(code, int) else None,
                )
            return payload.get("data") if isinstance(payload, dict) else payload

        raise ExternalAPIError("RAGFlow 请求重试耗尽", detail=f"{path} {last_error}")

    # ------------------------------------------------------------------
    # 知识库 / 文档
    # ------------------------------------------------------------------
    def list_datasets(self) -> list[dict]:
        data = self._request("GET", "/api/v1/datasets", params={"page": 1, "page_size": 100})
        return data if isinstance(data, list) else (data.get("data") or [])

    def get_or_create_dataset(
        self,
        name: str,
        *,
        description: str = "",
        embedding_model: str | None = None,
        chunk_method: str = "naive",
    ) -> str:
        """按名称取知识库 id，不存在则创建。"""
        for ds in self.list_datasets():
            if ds.get("name") == name:
                logger.info("复用已有知识库: %s (%s)", name, ds.get("id"))
                return str(ds.get("id"))

        body: dict = {
            "name": name,
            "chunk_method": chunk_method,
            "permission": "me",
            "description": description or "TikTok 日本站寝具店铺客服知识库",
        }
        if embedding_model:
            body["embedding_model"] = embedding_model
        created = self._request("POST", "/api/v1/datasets", json=body)
        if isinstance(created, list) and created:
            created = created[0]
        dataset_id = str((created or {}).get("id", ""))
        if not dataset_id:
            raise ExternalAPIError(f"创建知识库失败，未返回 id: {str(created)[:200]}")
        logger.info("已创建知识库: %s (%s)", name, dataset_id)
        return dataset_id

    def list_documents(self, dataset_id: str) -> list[dict]:
        """列出知识库全部文档（自动翻页）。

        注意不要相信响应里的 ``total``：实测它会漏算部分文档（真实踩坑），导致
        翻页提前终止 → 上层误判「远端缺文档」→ 重复上传出 "(1)" 后缀副本。
        这里以「本页不满 page_size」作为翻页终止条件，并加页数上限兜底。
        """
        docs: list[dict] = []
        page = 1
        while page <= 50:  # 上限兜底：100/页 * 50 = 5000 份，远超本项目规模
            data = self._request(
                "GET",
                f"/api/v1/datasets/{dataset_id}/documents",
                params={"page": page, "page_size": 100},
            )
            batch = (data or {}).get("docs") if isinstance(data, dict) else data
            batch = batch or []
            docs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return docs

    def list_chunks(
        self, dataset_id: str, document_id: str, *, limit: int | None = None
    ) -> list[dict]:
        """列出某文档的切片（按文档内顺序；``limit`` 给定时提前停止翻页）。"""
        chunks: list[dict] = []
        page = 1
        while page <= 50:
            data = self._request(
                "GET",
                f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks",
                params={"page": page, "page_size": 100},
            )
            batch = (data or {}).get("chunks") if isinstance(data, dict) else data
            batch = batch or []
            chunks.extend(batch)
            if len(batch) < 100 or (limit is not None and len(chunks) >= limit):
                break
            page += 1
        return chunks if limit is None else chunks[:limit]

    def rename_document(self, dataset_id: str, document_id: str, name: str) -> None:
        """重命名远端文档。

        用途：RAGFlow 对同名上传会自动加 "(1)" 后缀。删掉多余副本后，还要把
        幸存者改回规范名——否则「按文件名锚定商品文档」这条确定性链路会失效
        （见 ``scripts/reconcile_ragflow.py``）。
        """
        self._request(
            "PUT",
            f"/api/v1/datasets/{dataset_id}/documents/{document_id}",
            json={"name": name},
        )
        logger.info("已重命名远端文档 %s -> %s", document_id, name)

    def upload(self, dataset_id: str, paths: list[Path]) -> list[dict]:
        """批量上传本地文件（multipart）。"""
        if not paths:
            return []
        files = [
            (
                "file",
                (
                    p.name,
                    p.read_bytes(),
                    mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                ),
            )
            for p in paths
        ]
        data = self._request("POST", f"/api/v1/datasets/{dataset_id}/documents", files=files)
        return data if isinstance(data, list) else [data]

    def parse(self, dataset_id: str, document_ids: list[str]) -> None:
        """触发解析。

        RAGFlow 在文档上传后**会自动开始解析**，因此再显式触发时，若文档已不在
        ``UNSTART`` 状态，服务端会返回业务错误 ``code=102``。这不是失败——我们想要
        的「解析已经在跑」恰恰已经发生，所以这里把它当作可容忍的状态冲突吞掉；
        真正的失败（权限、参数错误等）仍然向上抛。
        """
        if not document_ids:
            return
        try:
            self._request(
                "POST",
                f"/api/v1/datasets/{dataset_id}/chunks",
                json={"document_ids": list(document_ids)},
            )
        except ExternalAPIError as exc:
            if exc.remote_code == ALREADY_PARSING_CODE:
                logger.info("文档已在解析中，跳过重复触发（%d 份）", len(document_ids))
                return
            raise
        logger.info("已触发解析 %d 份文档", len(document_ids))

    def wait_for_parse(
        self,
        dataset_id: str,
        document_ids: list[str],
        *,
        timeout: float = 900.0,
        interval: float = 3.0,
    ) -> dict[str, str]:
        """轮询直到全部解析结束或超时，返回 {文件名: 状态}。"""
        if not document_ids:
            return {}
        wanted = set(document_ids)
        deadline = time.time() + timeout
        status: dict[str, str] = {}

        while True:
            docs = [d for d in self.list_documents(dataset_id) if d.get("id") in wanted]
            status = {d.get("name", d.get("id", "?")): d.get("run", "UNKNOWN") for d in docs}
            pending = [n for n, s in status.items() if s not in TERMINAL_STATES]
            if not pending:
                logger.info("解析完成: %s", status)
                return status
            if time.time() > deadline:
                logger.warning(
                    "等待解析超时（%.0fs），仍在处理: %s", timeout, pending[:5]
                )
                return status
            logger.info("解析中 %d 份待完成（如 %s），%.0fs 后重试", len(pending), pending[:2], interval)
            time.sleep(interval)

    def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        if not document_ids:
            return
        self._request(
            "DELETE",
            f"/api/v1/datasets/{dataset_id}/documents",
            json={"ids": list(document_ids)},
        )
        logger.info("已删除 %d 份远端文档", len(document_ids))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# ----------------------------------------------------------------------
# 高层：一条命令完成「本地文档 -> RAGFlow 可用知识库」
# ----------------------------------------------------------------------
def _strip_copy_suffix(name: str) -> str:
    """去掉 RAGFlow 同名上传自动加的副本后缀：'KB-0001(1).md' -> 'KB-0001.md'。"""
    return re.sub(r"\s*\(\d+\)(?=\.[^.]+$)", "", name)


def _stale_document_ids(docs: list[dict], local_sizes: dict[str, int]) -> list[str]:
    """找出内容已过期的远端文档 id：基准名在本地、但 size 与本地不一致的副本。"""
    stale: list[str] = []
    for d in docs:
        base = _strip_copy_suffix(str(d.get("name")))
        expected = local_sizes.get(base)
        if expected is not None and d.get("size") != expected and d.get("id"):
            stale.append(str(d["id"]))
    return stale


def backup_remote_documents(
    ingestor: RagflowIngestor,
    dataset_id: str,
    docs: list[dict],
    backup_dir: Path,
) -> Path:
    """删除前把远端文档落盘备份（只备份正文，够用且体积可控）。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = backup_dir / f"ragflow-docs-{stamp}.json"
    payload = [
        {"id": d.get("id"), "name": d.get("name"), "run": d.get("run"), "chunk_count": d.get("chunk_count")}
        for d in docs
    ]
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("远端文档清单已备份: %s", out)
    return out


def ingest_knowledge_base(
    settings: Settings,
    *,
    replace: bool = False,
    prune: bool = False,
    wait: bool = True,
    timeout: float = 900.0,
) -> IngestReport:
    """把 ``data/knowledge_base/`` 下的文档灌入配置指定的 RAGFlow 知识库。

    Args:
        replace: 同名文档是否重新上传（默认跳过，保证幂等）
        prune:   是否删除「本地已不存在」的远端文档（默认否；开启前会自动备份）
        wait:    是否等待 deepdoc 解析完成
        timeout: 等待解析的上限秒数
    """
    from customer_support.knowledge.loader import iter_knowledge_files

    report = IngestReport()
    dataset_name = settings.ragflow.dataset_ids[0]
    ingestor = RagflowIngestor(
        settings.ragflow.base_url,
        settings.ragflow.api_key,
        timeout=max(settings.ragflow.timeout, 60.0),
        max_retries=settings.ragflow.max_retries,
    )

    try:
        report.dataset_id = ingestor.get_or_create_dataset(dataset_name)
        local_files = iter_knowledge_files(
            settings.knowledge.knowledge_base_dir, settings.knowledge.supported_extensions
        )
        if not local_files:
            raise KnowledgeError(
                f"知识库目录里没有可入库的文档: {settings.knowledge.knowledge_base_dir}"
            )
        local_names = {p.name for p in local_files}
        logger.info("本地待入库文档 %d 份", len(local_files))

        remote_docs = ingestor.list_documents(report.dataset_id)
        remote_by_name = {d.get("name"): d for d in remote_docs}

        # replace 语义 = 「以本地为准」：内容没变的文档**跳过上传**——RAGFlow 对同名
        # 上传会自动生成 "(1)" 后缀副本，盲目全量重传会让副本越滚越多（真实踩坑）。
        # 只有新增或内容变化（size 不同）的文档才上传，旧版本副本随后统一删除。
        local_sizes = {p.name: p.stat().st_size for p in local_files}
        if replace:
            to_upload = [
                p
                for p in local_files
                if p.name not in remote_by_name
                or remote_by_name[p.name].get("size") != local_sizes[p.name]
            ]
        else:
            to_upload = [p for p in local_files if p.name not in remote_by_name]
        report.skipped = sorted(p.name for p in local_files if p.name not in {q.name for q in to_upload})

        # 分批上传：单次请求体过大会被网关截断
        batch_size = 20
        for i in range(0, len(to_upload), batch_size):
            batch = to_upload[i : i + batch_size]
            try:
                ingestor.upload(report.dataset_id, batch)
                report.uploaded.extend(p.name for p in batch)
                logger.info("已上传 %d/%d 份", min(i + batch_size, len(to_upload)), len(to_upload))
            except Exception as exc:  # noqa: BLE001
                report.failed.append({"batch": [p.name for p in batch], "error": str(exc)})
                logger.error("上传失败: %s", exc)

        # replace 模式：清理内容已过期的旧版本副本（可能带 "(1)" 后缀，见上方说明）。
        # 解析中的文档会被 RAGFlow 拒绝删除（业务码 102），给一段重试窗口；仍失败则
        # 提示用 scripts/reconcile_ragflow.py 收敛，不阻断整个 ingest。
        if replace:
            docs_now = ingestor.list_documents(report.dataset_id)
            stale_ids = _stale_document_ids(docs_now, local_sizes)
            stale_names = {
                str(d.get("name")) for d in docs_now if str(d.get("id")) in stale_ids
            }
            deadline = time.time() + 90
            while stale_ids:
                try:
                    ingestor.delete_documents(report.dataset_id, stale_ids)
                    report.deleted.extend(sorted(stale_names))
                    break
                except ExternalAPIError as exc:
                    if str(ALREADY_PARSING_CODE) not in str(exc) or time.time() > deadline:
                        logger.warning(
                            "旧版本清理未完成，可运行 scripts/reconcile_ragflow.py 收敛：%s", exc
                        )
                        break
                    time.sleep(10)
                    docs_now = ingestor.list_documents(report.dataset_id)
                    stale_ids = _stale_document_ids(docs_now, local_sizes)
                    stale_names = {
                        str(d.get("name")) for d in docs_now if str(d.get("id")) in stale_ids
                    }

        # 清理本地已不存在的远端文档
        if prune:
            extra = [d for d in remote_docs if d.get("name") not in local_names]
            if extra:
                backup_remote_documents(
                    ingestor,
                    report.dataset_id,
                    extra,
                    settings.knowledge.knowledge_base_dir.parent / "raw" / "ragflow_backup",
                )
                extra_ids = [str(d.get("id")) for d in extra if d.get("id")]
                ingestor.delete_documents(report.dataset_id, extra_ids)
                report.deleted = sorted(str(d.get("name")) for d in extra)

        # 解析：新上传的 + 已存在但未解析成功的
        #
        # 注意 RAGFlow 上传后会自动开始解析，因此这里区分两件事：
        # - 「需要显式触发解析」的：UNSTART / FAIL / CANCEL；
        # - 「需要等待完成」的：上面这些 + 当前正在 RUNNING 的。
        # 这样重复执行 ingest 时既能幂等跳过上传，又能把上一轮没跑完的解析等到终态。
        after = ingestor.list_documents(report.dataset_id)
        need_parse = {
            str(d.get("id"))
            for d in after
            if d.get("name") in local_names and d.get("run") in {"UNSTART", "FAIL", "CANCEL"}
        }
        wait_ids = need_parse | {
            str(d.get("id"))
            for d in after
            if d.get("name") in local_names and d.get("run") not in TERMINAL_STATES
        }
        ingestor.parse(report.dataset_id, sorted(need_parse))

        if wait and wait_ids:
            report.parse_status = ingestor.wait_for_parse(
                report.dataset_id, sorted(wait_ids), timeout=timeout
            )
        return report
    finally:
        ingestor.close()
