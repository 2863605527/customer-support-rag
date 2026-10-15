"""端到端验证「运营控制台」这一层：工单闭环、会话复盘、质量反馈、指标、限流与安全头。

跑法（容器已在 8002 运行）：
    .venv/Scripts/python.exe scripts/verify_console.py

脚本会真实走一遍运营动作（提问 → 落库 → 评价 → 工单流转 → 读指标），
但**不依赖**具体回答内容（LLM 欠费时也能验证控制台链路是否通）。
"""
from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8002"
ADMIN = {"email": "admin@kurashi-bedding.jp", "password": "Kurashi-Admin-2026!"}

# 让会话带上可识别的标记，便于在列表里找到本次验证产生的数据
SESSION = "verify-console-1"

ok = 0
bad = 0


def check(name: str, passed: bool, extra: str = "") -> None:
    global ok, bad
    mark = "✓" if passed else "✗"
    if passed:
        ok += 1
    else:
        bad += 1
    print(f"  {mark} {name}" + (f" — {extra}" if extra else ""), flush=True)


def main() -> int:
    client = httpx.Client(timeout=180.0)

    print("\n=== 1. 探针与安全头 ===", flush=True)
    health = client.get(f"{BASE}/healthz")
    check("healthz 200", health.status_code == 200)
    check("X-Content-Type-Options", health.headers.get("x-content-type-options") == "nosniff")
    check("X-Frame-Options", health.headers.get("x-frame-options") == "DENY")
    check("CSP 存在", "content-security-policy" in {k.lower() for k in health.headers})

    ready = client.get(f"{BASE}/readyz")
    body = ready.json()
    check("readyz 返回 llm 健康块", "llm" in body.get("checks", {}),
          f"ready={body.get('ready')} llm.ok={body.get('checks', {}).get('llm', {}).get('ok')}")
    check("readyz 返回 retrieval.ok", "ok" in body.get("checks", {}).get("retrieval", {}),
          f"docs={body.get('checks', {}).get('retrieval', {}).get('count')}")

    metrics = client.get(f"{BASE}/metrics")
    check("metrics 200 + 含 cs_ 指标",
          metrics.status_code == 200 and "cs_" in metrics.text,
          metrics.text.splitlines()[0] if metrics.text else "(空)")

    print("\n=== 2. 登录 ===", flush=True)
    login = client.post(f"{BASE}/api/v1/auth/login", json=ADMIN)
    check("管理员登录 200", login.status_code == 200, f"HTTP {login.status_code}")
    if login.status_code != 200:
        print(login.text[:300])
        return 2
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    print("\n=== 3. 提问并落库 ===", flush=True)
    chat = client.post(
        f"{BASE}/api/v1/chat",
        json={"question": "返品はできますか？", "session_id": SESSION, "buyer_id": "verify-bot"},
        headers=auth,
    )
    check("POST /chat 200", chat.status_code == 200, f"HTTP {chat.status_code}")
    result = chat.json()
    trace_id = result.get("trace_id", "")
    print(f"    source={result.get('source')} need_human={result.get('need_human')} "
          f"trace={trace_id}", flush=True)

    convs = client.get(f"{BASE}/api/v1/ops/conversations", params={"search": SESSION}, headers=auth)
    listed = convs.json()
    check("会话已落库（可在控制台检索到）", listed.get("total", 0) >= 1,
          f"total={listed.get('total')}")
    if listed.get("total"):
        item = listed["items"][0]
        check("会话摘要含最近问题", bool(item.get("last_question")),
              f"msgs={item.get('message_count')} handoffs={item.get('handoff_count')}")
        detail = client.get(f"{BASE}/api/v1/ops/conversations/{SESSION}", headers=auth).json()
        roles = [m["role"] for m in detail.get("messages", [])]
        check("会话详情为 一问一答", roles[:2] == ["buyer", "agent"], f"roles={roles[:4]}")

    print("\n=== 3.5 触发一次转人工（为工单闭环造数据）===", flush=True)
    # 高风险诉求必须转人工并建工单——这里刻意用最典型的暴言场景
    hr = client.post(
        f"{BASE}/api/v1/chat",
        json={
            "question": "商品が壊れていました。最悪です、今すぐ返金しろ！",
            "session_id": f"{SESSION}-handoff",
            "buyer_id": "verify-bot",
        },
        headers=auth,
    )
    check("高风险提问 200", hr.status_code == 200, f"HTTP {hr.status_code}")
    hr_result = hr.json() if hr.status_code == 200 else {}
    handoff = hr_result.get("need_human") is True
    print(f"    need_human={hr_result.get('need_human')} "
          f"reason={hr_result.get('human_reason')} ticket={hr_result.get('ticket_id')}", flush=True)
    check("高风险被转人工并建工单", handoff and bool(hr_result.get("ticket_id")),
          f"reason={hr_result.get('human_reason')}")

    print("\n=== 4. 回答质量反馈 ===", flush=True)
    fb = client.post(
        f"{BASE}/api/v1/feedback",
        json={
            "trace_id": trace_id or "verify-trace",
            "rating": "down",
            "reasons": ["missing_info"],
            "comment": "端到端验证产生的样本",
            "question": "返品はできますか？",
            "answer": result.get("answer", ""),
            "source": result.get("source", ""),
        },
        headers=auth,
    )
    check("提交差评 201", fb.status_code == 201, f"HTTP {fb.status_code}")
    fb_bad = client.post(
        f"{BASE}/api/v1/feedback",
        json={"trace_id": "verify-bad", "rating": "down", "reasons": []},
        headers=auth,
    )
    check("差评缺原因被拒（400）", fb_bad.status_code == 400, f"HTTP {fb_bad.status_code}")
    stats = client.get(f"{BASE}/api/v1/ops/feedback/stats", headers=auth).json()
    check("反馈统计可读", "positive_rate" in stats and "by_reason" in stats,
          f"total={stats.get('total')} down={stats.get('down')}")
    ds = client.get(f"{BASE}/api/v1/ops/feedback/dataset", headers=auth)
    check("评测集导出（管理员）", ds.status_code == 200 and "samples" in ds.json(),
          f"total={ds.json().get('total') if ds.status_code == 200 else '-'}")

    print("\n=== 5. 工单闭环 ===", flush=True)
    tickets = client.get(f"{BASE}/api/v1/ops/tickets", params={"limit": 5}, headers=auth).json()
    check("工单列表可读", "items" in tickets and "total" in tickets, f"total={tickets.get('total')}")
    tstats = client.get(f"{BASE}/api/v1/ops/tickets/stats", headers=auth).json()
    check("工单看板含 SLA 字段",
          "backlog" in tstats and "avg_first_response_minutes" in tstats,
          f"backlog={tstats.get('backlog')} overdue={tstats.get('overdue')}")

    created_ticket = None
    if tickets.get("items"):
        tid = tickets["items"][0]["ticket_id"]
        created_ticket = tid
        patch = client.patch(
            f"{BASE}/api/v1/ops/tickets/{tid}",
            json={"status": "in_progress", "assignee": "admin@kurashi-bedding.jp", "priority": "high"},
            headers=auth,
        )
        check("工单状态/指派更新", patch.status_code == 200 and patch.json()["status"] == "in_progress",
              f"HTTP {patch.status_code}")
        reply = client.post(
            f"{BASE}/api/v1/ops/tickets/{tid}/replies",
            json={"body": "確認のうえご連絡します", "internal": False},
            headers=auth,
        )
        check("人工回复落库", reply.status_code == 201 and len(reply.json()["replies"]) >= 1,
              f"first_response={bool(reply.json().get('first_response_at')) if reply.status_code == 201 else '-'}")
        close = client.patch(f"{BASE}/api/v1/ops/tickets/{tid}", json={"status": "resolved"}, headers=auth)
        check("工单结单并记录解决时间",
              close.status_code == 200 and close.json().get("resolved_at") is not None)
    else:
        print("    （暂无工单：本次提问未触发转人工，跳过流转验证）", flush=True)

    print("\n=== 6. 运营指标 ===", flush=True)
    an = client.get(f"{BASE}/api/v1/ops/analytics", params={"days": 7}, headers=auth)
    check("analytics 200", an.status_code == 200, f"HTTP {an.status_code}")
    if an.status_code == 200:
        ov = an.json()
        conv = ov["conversation"]
        check("指标含自助解决率", "containment_rate" in conv,
              f"containment={conv.get('containment_rate')} answers={conv.get('answers')}")
        check("指标含按天趋势", isinstance(conv.get("daily"), list), f"days={len(conv.get('daily', []))}")
        check("指标含熔断与 LLM 健康", "breakers" in ov and "llm" in ov)

    print("\n=== 7. 限流与权限 ===", flush=True)
    nope = client.get(f"{BASE}/api/v1/ops/analytics")
    check("无令牌访问被拒（401）", nope.status_code == 401, f"HTTP {nope.status_code}")

    if created_ticket:
        # 工单已被结单，这里只验证读权限对匿名关闭
        anon = client.get(f"{BASE}/api/v1/ops/tickets/{created_ticket}")
        check("匿名读工单详情被拒", anon.status_code == 401, f"HTTP {anon.status_code}")

    print(f"\n=== 汇总：{ok} 通过 / {bad} 失败 ===", flush=True)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
