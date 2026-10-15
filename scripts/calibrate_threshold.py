"""检索阈值标定：用带标注的问题集找出 ``RETRIEVAL_SCORE_THRESHOLD`` 的合理取值。

为什么需要这个脚本：

RAGFlow / 向量库返回的 ``similarity`` 量纲与分布因平台、embedding 模型、
混合检索权重而异——**不能拍脑袋写 0.8 或 0.35**。阈值定太高会把正常问题
全判成「资料不足」（客服系统里表现为「永远在转人工」），定太低又会让不相关问题
穿过闸门、污染回答。正确做法是用一小份标注集把阈值**量出来**。

评测口径：
- ``relevant=true`` 的问题：命中 ``expect_sources`` 才算真阳（Recall）；
- ``relevant=false`` 的问题：任何命中都算误检（FPR）；
- 阈值扫描取 Youden's J = TPR - FPR 最大的点，平局时偏向**更高阈值**（安全优先，
  因为漏检的代价是转人工，误检的代价是可能的错误回答）。

用法::

    python scripts/calibrate_threshold.py                 # 只看建议值
    python scripts/calibrate_threshold.py --verbose       # 附每个问题的得分明细
    python scripts/calibrate_threshold.py --check --min-recall 0.9   # CI 门禁
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))


@dataclass
class QueryOutcome:
    """单个问题的检索结果。"""

    query: str
    relevant: bool
    max_score: float
    hits: list[tuple[float, str]]
    retrieved_expected: bool  # 期望来源是否出现在候选里（与阈值无关，衡量召回能力）


def load_eval(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["queries"]


def evaluate(queries: list[dict], top_k: int, verbose: bool = False) -> list[QueryOutcome]:
    from customer_support.api.deps import get_config
    from customer_support.rag.retriever import Retriever

    settings = get_config()
    # 用 threshold=0 走完整检索链路（含超取与来源去重），只是不施加阈值，
    # 这样标定出来的阈值与实际线上链路是同一套候选集。
    from customer_support.rag.factory import build_retrieval_backend

    retriever = Retriever(
        build_retrieval_backend(settings),
        top_k=top_k,
        score_threshold=0.0,
        max_per_source=settings.retrieval.max_per_source,
    )

    outcomes: list[QueryOutcome] = []
    for item in queries:
        query = item["query"]
        expected = item.get("expect_sources") or []
        hits = retriever.retrieve(query, top_k=top_k)
        max_score = max((h.score for h in hits), default=0.0)
        retrieved_expected = any(
            any(s in (h.source or "") for s in expected) for h in hits
        ) if expected else False

        outcomes.append(
            QueryOutcome(
                query=query,
                relevant=bool(item.get("relevant")),
                max_score=max_score,
                hits=[(round(h.score, 4), h.source or "?") for h in hits],
                retrieved_expected=retrieved_expected,
            )
        )
        if verbose:
            flag = "REL " if item.get("relevant") else "IRR "
            mark = "ok" if (retrieved_expected if expected else True) else "MISS"
            print(f"[{flag}] max={max_score:.4f} {mark}  {query}")
            for score, source in outcomes[-1].hits:
                print(f"          {score:.4f}  {source}")
    return outcomes


def scan_threshold(outcomes: list[QueryOutcome], step: float = 0.01) -> list[dict]:
    """在 [0, 1) 上扫描阈值，返回每档的 TPR / FPR / Youden J。"""
    rel = [o for o in outcomes if o.relevant]
    irr = [o for o in outcomes if not o.relevant]
    rows: list[dict] = []
    steps = int(1 / step)
    for i in range(steps):
        thr = round(i * step, 4)
        tp = sum(1 for o in rel if o.max_score >= thr)
        fp = sum(1 for o in irr if o.max_score >= thr)
        tpr = tp / len(rel) if rel else 0.0
        fpr = fp / len(irr) if irr else 0.0
        rows.append({"threshold": thr, "tp": tp, "fp": fp, "tpr": tpr, "fpr": fpr, "j": tpr - fpr})
    return rows


def pick(rows: list[dict]) -> dict:
    """Youden's J 最大；平局取更高阈值（安全优先）。"""
    return max(rows, key=lambda r: (round(r["j"], 6), round(r["tpr"], 6), r["threshold"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="标定 RAG 检索相似度阈值")
    parser.add_argument("--eval", type=Path, default=ROOT_DIR / "data" / "eval" / "retrieval_eval.json")
    parser.add_argument("--top-k", type=int, default=None, help="检索条数（默认取配置值）")
    parser.add_argument("--step", type=float, default=0.01)
    parser.add_argument("--verbose", action="store_true", help="打印每个问题的得分明细")
    parser.add_argument("--check", action="store_true", help="CI 模式：召回率不达标则退出码 1")
    parser.add_argument("--min-recall", type=float, default=0.85, help="--check 模式下的召回率下限")
    args = parser.parse_args()

    if not args.eval.exists():
        print(f"找不到评测集: {args.eval}")
        return 2

    from customer_support.api.deps import get_config

    settings = get_config()
    top_k = args.top_k or settings.retrieval.top_k
    print(f"评测集: {args.eval}")
    print(f"后端:   {settings.retrieval.backend} | top_k={top_k} | 当前阈值={settings.retrieval.score_threshold}")
    print("-" * 72)

    outcomes = evaluate(load_eval(args.eval), top_k, verbose=args.verbose)

    rel = [o for o in outcomes if o.relevant]
    irr = [o for o in outcomes if not o.relevant]
    retrieved = sum(1 for o in rel if o.retrieved_expected)

    print("-" * 72)
    print(f"标注问题: 相关 {len(rel)} / 无关 {len(irr)}")
    print(f"检索召回（期望来源是否出现在候选里，与阈值无关）: {retrieved}/{len(rel)}")
    for o in rel:
        if not o.retrieved_expected:
            print(f"  ! 未召回期望来源: {o.query}（max={o.max_score:.4f}）")

    rows = scan_threshold(outcomes, args.step)
    best = pick(rows)

    print("-" * 72)
    print("阈值扫描（节选）")
    print(f"{'阈值':>6}  {'命中(TP)':>9}  {'误检(FP)':>9}  {'召回率':>7}  {'误检率':>7}  {'J':>7}")
    interesting = {round(best["threshold"], 4), 0.0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5}
    interesting.add(round(settings.retrieval.score_threshold, 4))
    for r in rows:
        if r["threshold"] in interesting:
            print(
                f"{r['threshold']:>6.2f}  {r['tp']:>4}/{len(rel):<4}  {r['fp']:>4}/{len(irr):<4}  "
                f"{r['tpr']:>7.2f}  {r['fpr']:>7.2f}  {r['j']:>7.2f}"
            )

    print("-" * 72)
    print(
        f"建议 RETRIEVAL_SCORE_THRESHOLD={best['threshold']:.2f}"
        f"（召回 {best['tp']}/{len(rel)}，误检 {best['fp']}/{len(irr)}，J={best['j']:.2f}）"
    )
    print(f"当前配置 RETRIEVAL_SCORE_THRESHOLD={settings.retrieval.score_threshold:.2f}")
    if abs(best["threshold"] - settings.retrieval.score_threshold) > 0.02:
        print("提示：两者差异较大，建议按上方建议值更新 .env 后重跑本脚本确认。")

    recall_at_current = sum(
        1 for o in rel if o.max_score >= settings.retrieval.score_threshold
    ) / len(rel) if rel else 0.0
    print(f"当前阈值下的召回率: {recall_at_current:.0%}")

    if args.check and recall_at_current < args.min_recall:
        print(f"CI 门禁失败：召回率 {recall_at_current:.0%} < {args.min_recall:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
