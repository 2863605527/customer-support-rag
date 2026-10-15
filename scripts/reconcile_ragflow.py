"""把 RAGFlow 知识库收敛到与本地 knowledge_base 目录一一对应。

背景：``ingest --replace`` 先全量上传再删旧，而 RAGFlow 对同名上传会自动加
"(1)" 后缀；若旧文档解析中还会拒绝删除（业务码 102），造成同一文档多个副本。

本脚本做四件事（可重复执行，收敛为幂等）：
1. 等待所有解析中的文档进入终态（RUNNING / UNSTART → DONE / FAIL）；
2. 以本地 ``data/knowledge_base`` 的文件名为基准：
   - 与本地内容一致（size 相同）的副本优先保留；否则保留最新一份；
   - 其余副本（含 "(1)" 后缀副本与本地不存在的垃圾文档）全部删除；
3. 把幸存副本改回规范文件名（去掉 "(1)" 后缀）——商品链接问答要按
   ``KB-XXXX.md`` **精确**取回该商品文档（确定性锚定），名字不对这条链路就失效；
4. 对仍在 UNSTART 的文档触发解析并等待完成。

用法：python scripts/reconcile_ragflow.py [--yes]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from customer_support.config import get_settings  # noqa: E402
from customer_support.knowledge.loader import iter_knowledge_files  # noqa: E402
from customer_support.knowledge.ragflow_ingest import RagflowIngestor  # noqa: E402

_SUFFIX_RE = re.compile(r"\s*\(\d+\)(?=\.[^.]+$)")


def base_name(name: str) -> str:
    return _SUFFIX_RE.sub("", name)


def main() -> int:
    parser = argparse.ArgumentParser(description="收敛 RAGFlow 文档与本地知识库一致")
    parser.add_argument("--yes", action="store_true", help="跳过删除确认（自动化用）")
    args = parser.parse_args()

    settings = get_settings()
    ingestor = RagflowIngestor(
        settings.ragflow.base_url, settings.ragflow.api_key, timeout=60, max_retries=3
    )
    dataset_id = ingestor.get_or_create_dataset("tiktok_bedding_kb")
    local_dir = settings.knowledge.knowledge_base_dir
    # 用与入库同一套过滤规则（loader）判定「本地该有哪些文档」。
    # 之前这里自己写 rglob，会把手写的 README.md 也算进来，
    # 与 ingest 的认知不一致——收敛的基准必须和入库的基准是同一个。
    local_files = {
        p.name: p.stat().st_size
        for p in iter_knowledge_files(local_dir, settings.knowledge.supported_extensions)
    }
    print(f"[reconcile] 本地文档 {len(local_files)} 份")

    docs = ingestor.list_documents(dataset_id)
    print(f"[reconcile] 远端文档 {len(docs)} 份")

    # ---- 1. 等待解析终态（最长 10 分钟）----
    deadline = time.time() + 600
    while time.time() < deadline:
        running = [d for d in docs if d.get("run") in ("RUNNING", "UNSTART")]
        if not running:
            break
        print(f"[reconcile] 等待解析：{len(running)} 份进行中…")
        time.sleep(10)
        # 队列积压时这两个调用都容易超时（RAGFlow 侧正忙）。它们是等待环节，
        # 失败不该中断整次收敛——下一轮重试即可，所以在这里就地吞掉异常。
        to_parse = [str(d["id"]) for d in running if d.get("run") == "UNSTART"]
        if to_parse:
            try:
                ingestor.parse(dataset_id, to_parse)
            except Exception as exc:  # noqa: BLE001
                print(f"[reconcile] 触发解析失败（{len(to_parse)} 份）：{type(exc).__name__}，稍后重试")
        try:
            docs = ingestor.list_documents(dataset_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[reconcile] 拉取文档列表失败：{type(exc).__name__}，稍后重试")
    still = [d["name"] for d in docs if d.get("run") not in ("DONE", "FAIL", "")]
    if still:
        print(f"[reconcile] 警告：仍有未终态文档 {len(still)} 份（继续清理）")

    # ---- 2. 按基准名分组：每组保留一份、其余删除；幸存者改回规范名 ----
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        groups[base_name(str(d.get("name")))].append(d)

    keep_ids: list = []
    delete_ids: list[str] = []
    renames: list[tuple[str, str]] = []  # (document_id, 规范名)
    for base, copies in groups.items():
        if base not in local_files:  # 本地已不存在的远端垃圾文档
            delete_ids.extend(str(d["id"]) for d in copies)
            continue
        if len(copies) == 1:
            winner = copies[0]
        else:
            target_size = local_files[base]
            # 与本地同尺寸的副本优先（内容一致）；都不一致就取最新的一份
            pool = [d for d in copies if d.get("size") == target_size] or copies
            winner = sorted(pool, key=lambda d: d.get("create_time") or 0)[-1]
            delete_ids.extend(str(d["id"]) for d in copies if d["id"] != winner["id"])
        keep_ids.append(winner["id"])
        # 幸存的副本可能仍带着 "(1)" 后缀：改回规范名——买家贴链接时要按
        # KB-XXXX.md **精确**取回商品文档（确定性锚定），名字不对锚定就失效。
        if str(winner.get("name")) != base:
            renames.append((str(winner["id"]), base))

    print(f"[reconcile] 保留 {len(keep_ids)} 份，删除 {len(delete_ids)} 份，改名 {len(renames)} 份")
    if delete_ids:
        if not args.yes:
            confirm = input("确认删除以上副本？[y/N] ").strip().lower()
            if confirm != "y":
                print("[reconcile] 已取消")
                return 1
        for i in range(0, len(delete_ids), 20):
            batch = delete_ids[i : i + 20]
            try:
                ingestor.delete_documents(dataset_id, batch)
                print(f"[reconcile] 已删除 {min(i + len(batch), len(delete_ids))}/{len(delete_ids)}")
            except Exception as exc:  # noqa: BLE001 - 单批失败不放弃剩余批次
                print(f"[reconcile] 批次删除失败（稍后重跑即可）：{exc}")
            time.sleep(1.5)

    # ---- 2'. 改名必须放在删除之后：否则与同名幸存者冲突会被 RAGFlow 再加一次后缀 ----
    for doc_id, name in renames:
        try:
            ingestor.rename_document(dataset_id, doc_id, name)
        except Exception as exc:  # noqa: BLE001 - 单个改名失败不影响其余
            print(f"[reconcile] 改名失败 {doc_id} -> {name}: {exc}")

    # ---- 3. 收尾：对残留 UNSTART 触发解析 ----
    docs = ingestor.list_documents(dataset_id)
    unstarted = [str(d["id"]) for d in docs if d.get("run") == "UNSTART"]
    if unstarted:
        ingestor.parse(dataset_id, unstarted)
        print(f"[reconcile] 触发解析 {len(unstarted)} 份")
    final = ingestor.list_documents(dataset_id)
    not_done = [str(d.get("name")) for d in final if d.get("run") not in ("DONE",)]
    print(f"[reconcile] 完成：远端 {len(final)} 份；未 DONE {len(not_done)} 份 {not_done[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
