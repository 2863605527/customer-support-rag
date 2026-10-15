"""回答质量反馈存储（SQLite）。

关键行为：

- **幂等**：``trace_id`` 唯一，重复评价走 UPSERT，只保留最后一次；
- **可导出**：``export_dataset`` 输出 JSONL，字段就是 ``(question, answer, rating, reasons)``，
  可以直接当回归评测集跑（改检索阈值 / 换模型后对比好评率是否提升）；
- **可分源**：按 ``source``（knowledge / no_answer / human）分组统计好评率，
  一眼看出"是检索的问题还是生成的问题"。

必须过滤掉 LLM 生成的原始分块里的 prompt 注入内容——但反馈是人工提交的短文本，
这里仅做长度限制与空白清理，不做转义（存库参数化查询本身已防注入）。
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from customer_support.models.feedback import (
    DOWN_REASONS,
    UP_REASONS,
    FeedbackRecord,
    FeedbackStats,
    FeedbackSubmission,
)
from customer_support.store.base import BaseStore, from_iso, to_iso, utcnow

_ALL_REASONS = {**UP_REASONS, **DOWN_REASONS}


def _row_to_record(row: Any) -> FeedbackRecord:
    return FeedbackRecord(
        id=int(row["id"]),
        trace_id=row["trace_id"],
        session_id=row["session_id"],
        rating=row["rating"],
        reasons=json.loads(row["reasons_json"] or "[]"),
        comment=row["comment"],
        question=row["question"],
        answer=row["answer"],
        source=row["source"],
        created_at=from_iso(row["created_at"]) or utcnow(),
    )


class FeedbackStore(BaseStore):
    """反馈读写与统计。"""

    def submit(self, payload: FeedbackSubmission) -> FeedbackRecord:
        """写入或覆盖一条评价（trace_id 唯一）。"""
        now = utcnow()
        # 只保留已知标签，避免前端传入任意字符串污染统计口径
        reasons = [r for r in dict.fromkeys(payload.reasons) if r in _ALL_REASONS]
        comment = " ".join(payload.comment.split())[:500]
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO feedback (trace_id, session_id, rating, reasons_json, comment,
                                      question, answer, source, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    rating = excluded.rating,
                    reasons_json = excluded.reasons_json,
                    comment = excluded.comment,
                    source = excluded.source,
                    created_at = excluded.created_at
                """,
                (
                    payload.trace_id,
                    payload.session_id,
                    payload.rating,
                    json.dumps(reasons, ensure_ascii=False),
                    comment,
                    payload.question[:2000],
                    payload.answer[:4000],
                    payload.source,
                    to_iso(now),
                ),
            )
        record = self.get(payload.trace_id)
        assert record is not None
        return record

    def get(self, trace_id: str) -> FeedbackRecord | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM feedback WHERE trace_id = ?", (trace_id,)).fetchone()
        return _row_to_record(row) if row else None

    def stats(self, *, days: int = 30, recent_down_limit: int = 10) -> FeedbackStats:
        cutoff = to_iso(utcnow() - timedelta(days=days))
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE created_at >= ? ORDER BY id DESC", (cutoff,)
            ).fetchall()
        records = [_row_to_record(r) for r in rows]

        up = sum(1 for r in records if r.rating == "up")
        down = len(records) - up
        by_source: dict[str, dict[str, int]] = {}
        by_reason: dict[str, int] = {}
        for r in records:
            bucket = by_source.setdefault(r.source or "unknown", {"up": 0, "down": 0, "total": 0})
            bucket["total"] += 1
            bucket[r.rating] += 1
            if r.rating == "down":
                for reason in r.reasons or ["other"]:
                    by_reason[reason] = by_reason.get(reason, 0) + 1

        return FeedbackStats(
            total=len(records),
            up=up,
            down=down,
            positive_rate=round(up / len(records), 4) if records else None,
            by_source=by_source,
            by_reason=by_reason,
            recent_down=[r for r in records if r.rating == "down"][:recent_down_limit],
        )

    def export_dataset(self, *, days: int = 30, limit: int = 2000) -> list[dict[str, Any]]:
        """导出为回归评测集样本（含好评与差评，供离线评估对比）。"""
        cutoff = to_iso(utcnow() - timedelta(days=days))
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
                (cutoff, max(1, min(limit, 10000))),
            ).fetchall()
        dataset: list[dict[str, Any]] = []
        for row in rows:
            record = _row_to_record(row)
            dataset.append(
                {
                    "trace_id": record.trace_id,
                    "question": record.question,
                    "answer": record.answer,
                    "source": record.source,
                    "rating": record.rating,
                    "expected_ok": record.rating == "up",
                    "reasons": record.reasons,
                    "comment": record.comment,
                    "created_at": record.created_at.isoformat(),
                }
            )
        return dataset
