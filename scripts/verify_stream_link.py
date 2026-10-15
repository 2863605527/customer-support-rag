"""端到端验证：SSE 思考过程事件 + 商品链接问题能否答出材质/尺寸。

用法：.venv/Scripts/python.exe scripts/verify_stream_link.py
"""
import json
import sys

import httpx

BASE = "http://127.0.0.1:8002"
ADMIN = {"email": "admin@kurashi-bedding.jp", "password": "Kurashi-Admin-2026!"}

QUESTION = (
    "https://detail.zhaojiafang.com/goods/11748832 "
    "この商品の素材は何ですか？あと、幅120cmのベッドでも使えますか？"
)
# 支持命令行传入自定义问题（回归验证普通问题用）
if len(sys.argv) > 1:
    QUESTION = sys.argv[1]


def frames(text: str):
    """把 SSE 原始文本拆成 (event, payload) 列表。"""
    out = []
    for block in text.split("\n\n"):
        event, data = "message", None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if data:
            out.append((event, json.loads(data)))
    return out


def main() -> int:
    client = httpx.Client(timeout=180.0)
    print("healthz:", client.get(f"{BASE}/healthz").status_code, flush=True)
    r = client.get(f"{BASE}/readyz")
    print("readyz:", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:400], flush=True)
    if not r.json().get("ready"):
        print("!! readyz 未就绪（RAGFlow 可能仍在预热），仍继续尝试问答", flush=True)

    login = client.post(f"{BASE}/api/v1/auth/login", json=ADMIN)
    print("login:", login.status_code, flush=True)
    if login.status_code != 200:
        print(login.text[:300])
        return 2
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

    body = {"question": QUESTION, "session_id": "verify-link-1", "buyer_id": "web-console"}
    raw = ""
    with client.stream("POST", f"{BASE}/api/v1/chat/stream", json=body, headers=headers) as resp:
        print("stream http:", resp.status_code, flush=True)
        for chunk in resp.iter_text():
            raw += chunk

    events = frames(raw)
    statuses = [p for e, p in events if e == "status"]
    deltas = [p.get("text", "") for e, p in events if e == "delta"]
    replaces = [p.get("text", "") for e, p in events if e == "replace"]
    dones = [p for e, p in events if e == "done"]

    print("\n=== 思考过程事件（status）===", flush=True)
    for s in statuses:
        print(" ", json.dumps(s, ensure_ascii=False), flush=True)
    print(f"\n帧统计：共 {len(events)} 帧；status={len(statuses)} delta={len(deltas)} "
          f"replace={len(replaces)} done={len(dones)}", flush=True)

    if not dones:
        print("!! 没有 done 帧")
        return 3
    result = dones[-1]["result"]
    print("\n=== 回答 ===", flush=True)
    print(result["answer"], flush=True)
    print("\nintent:", result["intent"], "confidence:", result["confidence"],
          "source:", result["source"], "need_human:", result["need_human"],
          "latency_ms:", result["latency_ms"], flush=True)
    for ref in result["references"]:
        print("  引用:", ref["source"], ref["score"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
