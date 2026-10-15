"""从找家纺网（zhaojiafang.com）公开接口采集床上用品货源数据。

用途：为客服 RAG 知识库提供**真实**商品数据（商品名 / 批发价 / 点击量 / 库存 /
店铺 / 图片），采集结果存档到 ``data/raw/zhaojiafang/``，再由
``scripts/build_kb_docs.py`` 生成知识库文档。

数据来源（均为站点公开前端接口，无登录）：
- ``GET https://api-pl-mobile.zhaojiafang.com/api/goods/hot_buy_goods``
    热卖商品榜，单次返回 60 条。
- ``GET https://api-pl-mobile.zhaojiafang.com/api/store_search/store_list``
    关键词店铺搜索，每页 10 家店铺，每家带 4 条主推商品。

设计要点：
- 仅用标准库（urllib），无需安装项目依赖即可运行；
- 内置限速与指数退避重试，避免给对方站点造成压力；
- 全量原始记录落盘，可复现、可审计；分类与选品规则显式可查。

用法::

    python scripts/harvest_zhaojiafang.py                 # 采集 + 选品，默认 60 个 SKU
    python scripts/harvest_zhaojiafang.py --target 100    # 指定 SKU 数量
    python scripts/harvest_zhaojiafang.py --offline       # 只用已有原始数据重新选品
"""
from __future__ import annotations

import argparse
import json
import random
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT_DIR / "data" / "raw" / "zhaojiafang"

API_BASE = "https://api-pl-mobile.zhaojiafang.com"
DETAIL_BASE = "https://detail.zhaojiafang.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 采集限速：每次请求之间的基础间隔（秒），加抖动避免规律性请求
REQUEST_INTERVAL = 0.9
MAX_RETRY = 3

ssl._create_default_https_context = ssl._create_unverified_context

# ---------------------------------------------------------------------------
# 品类定义
# ---------------------------------------------------------------------------
# key 为内部品类标识；keywords 用于命中商品名；queries 用于驱动店铺搜索接口。
CATEGORIES: dict[str, dict[str, Any]] = {
    "bedding_set": {
        "label_cn": "四件套/三件套",
        "label_ja": "寝具セット（4点/3点）",
        "keywords": ["四件套", "三件套"],
        "queries": ["四件套", "全棉四件套", "牛奶绒四件套", "床盖四件套", "印花四件套"],
        "quota": 16,
    },
    "quilt": {
        "label_cn": "被芯/被子",
        "label_ja": "掛け布団・中綿（羽毛/綿/ウール）",
        "keywords": ["被芯", "被子", "冬被", "春秋被", "夏被", "羊毛被", "大豆被", "棉花被"],
        "queries": ["被芯", "棉花被", "大豆纤维被", "羊毛被", "冬被加厚"],
        "quota": 14,
    },
    "pillow": {
        "label_cn": "枕芯/枕套",
        "label_ja": "枕・枕カバー",
        "keywords": ["枕芯", "枕头", "枕套"],
        "queries": ["枕芯", "枕套", "护颈枕"],
        "quota": 8,
    },
    "mattress_pad": {
        "label_cn": "床垫/床褥/床盖",
        "label_ja": "マットレスパッド・敷きパッド",
        "keywords": ["床垫", "床褥", "床盖", "床护垫", "保护垫"],
        "queries": ["床垫床褥", "夹棉床盖", "防滑床垫"],
        "quota": 10,
    },
    "bed_sheet": {
        "label_cn": "床单/被套/床裙/床笠",
        "label_ja": "シーツ・布団カバー・ベッドスカート",
        "keywords": ["床单", "被套", "床裙", "床笠", "单件"],
        "queries": ["床单", "被套单件", "床裙", "床笠"],
        "quota": 8,
    },
    "mat": {
        "label_cn": "凉席/藤席",
        "label_ja": "ござ・涼感マット",
        "keywords": ["凉席", "藤席", "豆豆席", "席子", "冰丝席"],
        "queries": ["凉席", "冰丝席", "老粗布凉席", "藤席", "竹席"],
        "quota": 6,
    },
    "blanket": {
        "label_cn": "毛毯/珊瑚绒毯",
        "label_ja": "毛布・ブランケット",
        "keywords": ["毛毯", "珊瑚绒毯", "绒毯", "法兰绒毛毯", "拉舍尔"],
        "queries": ["毛毯", "珊瑚绒毛毯", "法兰绒毛毯", "拉舍尔毛毯"],
        "quota": 6,
    },
    "accessory": {
        "label_cn": "蚊帐/其他床品配件",
        "label_ja": "蚊帳・その他寝具アクセサリ",
        "keywords": ["蚊帐", "抱枕", "靠垫", "眼罩", "电热毯"],
        "queries": ["蚊帐", "抱枕套"],
        "quota": 4,
    },
}

# 明显不适合作为零售在售商品（样品、配件单只、下架风格）的过滤词
EXCLUDE_KEYWORDS = ["运费", "差价", "补拍", "专拍", "链接", "配件", "说明"]

# 价格合理性区间（人民币，批发口径）——过滤明显异常的数据
PRICE_MIN, PRICE_MAX = 3.0, 3000.0

# 每个品类在最终选品中至少保留的 SKU 数（避免长尾品类被热度淹没）
MIN_PER_CATEGORY = 3


def log(msg: str) -> None:
    print(f"[harvest] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_get_json(url: str) -> dict[str, Any] | None:
    """GET 一个 JSON 接口，带重试与指数退避。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
        "Referer": "https://search.zhaojiafang.com/",
    }
    for attempt in range(1, MAX_RETRY + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read().decode("utf-8", "ignore")
            data = json.loads(body)
            if data.get("ResponseStatus") not in (0, None):
                log(f"  接口返回业务错误 status={data.get('ResponseStatus')} {url}")
                return None
            return data
        except Exception as exc:  # noqa: BLE001
            wait = 1.5 * attempt + random.random()
            log(f"  请求失败({attempt}/{MAX_RETRY}) {type(exc).__name__}: {exc} -> {wait:.1f}s 后重试")
            if attempt == MAX_RETRY:
                return None
            time.sleep(wait)
    return None


def polite_sleep() -> None:
    time.sleep(REQUEST_INTERVAL + random.random() * 0.4)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
_PRICE_RE = re.compile(r"[\d.]+")


def parse_price(raw: Any) -> float | None:
    """把 '¥160.00' / '160.00' / 160 统一成 float。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = _PRICE_RE.search(str(raw).replace(",", ""))
    return float(m.group()) if m else None


def classify(name: str) -> str | None:
    """按商品名判断品类；命中优先级按 CATEGORIES 顺序。"""
    for key, cfg in CATEGORIES.items():
        if any(kw in name for kw in cfg["keywords"]):
            return key
    return None


def normalize_good(raw: dict[str, Any], *, source: str, query: str | None) -> dict[str, Any] | None:
    """把不同接口返回的商品结构归一化。"""
    name = (raw.get("goods_name") or "").strip()
    goods_id = raw.get("goods_id")
    if not name or not goods_id:
        return None
    if any(bad in name for bad in EXCLUDE_KEYWORDS):
        return None

    price = parse_price(raw.get("goods_price"))
    if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
        return None

    category = classify(name)
    if category is None:
        return None

    image = raw.get("goods_image_url") or ""
    if not image and raw.get("goods_image"):
        image = f"https://imgniu.zhaojiafang.com/store/goods/{raw.get('store_id')}/{raw['goods_image']}"

    stock = raw.get("num")
    try:
        stock_int = int(str(stock).strip()) if stock is not None else None
    except (TypeError, ValueError):
        stock_int = None

    return {
        "goods_id": int(goods_id),
        "goods_commonid": raw.get("goods_commonid"),
        "goods_name": name,
        "category": category,
        "category_cn": CATEGORIES[category]["label_cn"],
        "price_cny_wholesale": round(price, 2),
        "price_raw": raw.get("goods_price"),
        "clicks": raw.get("common_click") or raw.get("goods_click") or 0,
        "stock": stock_int,
        "store_id": raw.get("store_id"),
        "store_name": (raw.get("store_name") or "").strip(),
        "image_url": image,
        "detail_url": f"{DETAIL_BASE}/goods/{goods_id}",
        "tags": raw.get("tags") or [],
        "added_at": raw.get("goods_addtime"),
        "source": source,
        "query": query,
    }


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def harvest_hot_goods() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """采集热卖商品榜。"""
    log("采集热卖榜 /api/goods/hot_buy_goods")
    url = f"{API_BASE}/api/goods/hot_buy_goods"
    data = http_get_json(url)
    if not data:
        return [], {"endpoint": url, "ok": False}
    items = (data.get("datas") or {}).get("data") or []
    out = [g for g in (normalize_good(r, source="hot_buy_goods", query=None) for r in items) if g]
    log(f"  热卖榜原始 {len(items)} 条，归一化后 {len(out)} 条")
    return out, {"endpoint": url, "ok": True, "raw_count": len(items)}

def harvest_store_search(max_pages: int = 2) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """按品类关键词搜索店铺，并从中提取店铺主推商品。"""
    goods: list[dict[str, Any]] = []
    stores: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    seen_stores: set[int] = set()

    for cat_key, cfg in CATEGORIES.items():
        for kw in cfg["queries"]:
            for page in range(1, max_pages + 1):
                url = (
                    f"{API_BASE}/api/store_search/store_list"
                    f"?keyword={urllib.parse.quote(kw)}&page={page}"
                )
                data = http_get_json(url)
                calls.append({"endpoint": url, "ok": bool(data), "category": cat_key, "keyword": kw, "page": page})
                if not data:
                    polite_sleep()
                    continue
                store_rows = ((data.get("datas") or {}).get("store_data") or {}).get("data") or []
                if not store_rows:
                    polite_sleep()
                    break
                for row in store_rows:
                    sid = row.get("store_id")
                    if sid and sid not in seen_stores:
                        seen_stores.add(sid)
                        stores.append({
                            "store_id": sid,
                            "store_name": row.get("store_name"),
                            "main_business": row.get("store_zy"),
                            "phone": row.get("store_phone"),
                            "address": row.get("store_address"),
                            "sales": row.get("store_sales"),
                            "supports_dropship": row.get("store_daifa"),
                        })
                    for rec in row.get("recommend") or []:
                        g = normalize_good(rec, source="store_list", query=kw)
                        if g:
                            # recommend 列表里不带店铺名，补上外层店铺信息
                            if not g.get("store_name"):
                                g["store_name"] = row.get("store_name") or ""
                            goods.append(g)
                log(f"  {cat_key}/{kw} p{page}: 店铺 {len(store_rows)} 家，累计候选商品 {len(goods)}")
                polite_sleep()
    return goods, stores, calls


# ---------------------------------------------------------------------------
# 选品
# ---------------------------------------------------------------------------
def select_products(candidates: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """去重 + 按品类配额比例选品，返回最终 SKU 列表。

    选品规则（显式可查，保证可复现）：
    1. 按 goods_id 去重，同一商品保留信息更全的一条；
    2. 每个品类内按点击量降序（点击量是站点公开的热度信号）；
    3. 第一轮按配额比例分配名额，但不超过该品类实际可得数量——保证小品类不会被
       大品类的热度淹没（否则凉席、毛毯这类长尾品类会全被挤掉）；
    4. 名额不足时从剩余池按点击量补齐；超员时从「超配额最严重」的品类里
       淘汰点击量最低的商品，且每个品类至少保留 MIN_PER_CATEGORY 个。
    """
    by_id: dict[int, dict[str, Any]] = {}
    for g in candidates:
        gid = g["goods_id"]
        prev = by_id.get(gid)
        # 同一商品被多次命中时，保留信息更全的一条（优先带店铺名的）
        if prev is None or (not prev.get("store_name") and g.get("store_name")):
            by_id[gid] = g
    uniq = list(by_id.values())
    log(f"去重后候选商品 {len(uniq)} 条（原始 {len(candidates)} 条）")

    # 每品类按点击量降序
    pools: dict[str, list[dict[str, Any]]] = {k: [] for k in CATEGORIES}
    for g in uniq:
        pools[g["category"]].append(g)
    for k in pools:
        pools[k].sort(key=lambda x: (x.get("clicks") or 0), reverse=True)

    quota_sum = sum(cfg["quota"] for cfg in CATEGORIES.values())
    selected: list[dict[str, Any]] = []

    # 第一轮：按配额比例分配（不超过实际可得）
    for key, cfg in CATEGORIES.items():
        want = max(MIN_PER_CATEGORY, round(target * cfg["quota"] / quota_sum))
        selected.extend(pools[key][:want])
        pools[key] = pools[key][want:]

    # 第二轮：名额不足 -> 从剩余池按点击量补齐
    if len(selected) < target:
        rest = sorted(
            (g for k in CATEGORIES for g in pools[k]),
            key=lambda x: (x.get("clicks") or 0),
            reverse=True,
        )
        selected.extend(rest[: target - len(selected)])

    # 第三轮：超员 -> 从「超配额最严重」的品类淘汰点击量最低的商品
    while len(selected) > target:
        counts = Counter(g["category"] for g in selected)
        candidates_to_trim = [
            k for k in CATEGORIES
            if counts.get(k, 0) > MIN_PER_CATEGORY and counts.get(k, 0) > 0
        ]
        if not candidates_to_trim:
            selected.sort(key=lambda x: (x.get("clicks") or 0), reverse=True)
            selected = selected[:target]
            break

        def surplus(key: str) -> float:
            return counts.get(key, 0) - target * CATEGORIES[key]["quota"] / quota_sum

        worst = max(candidates_to_trim, key=surplus)
        pool = [g for g in selected if g["category"] == worst]
        victim = min(pool, key=lambda g: (g.get("clicks") or 0))
        selected.remove(victim)

    selected.sort(key=lambda x: (x["category"], -(x.get("clicks") or 0)))
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="采集找家纺网床上用品货源数据")
    parser.add_argument("--target", type=int, default=60, help="最终选中的 SKU 数量")
    parser.add_argument("--pages", type=int, default=2, help="每个关键词翻页数")
    parser.add_argument("--offline", action="store_true", help="跳过网络采集，直接用已有原始数据重新选品")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if args.offline:
        src = RAW_DIR / "harvest_all.json"
        if not src.exists():
            log(f"离线模式需要已有数据：{src} 不存在")
            return 2
        payload = json.loads(src.read_text(encoding="utf-8"))
        candidates = payload["candidates"]
        stores = payload.get("stores", [])
        calls = payload.get("calls", [])
        log(f"离线模式：读入候选商品 {len(candidates)} 条")
    else:
        hot, hot_meta = harvest_hot_goods()
        polite_sleep()
        store_goods, stores, calls = harvest_store_search(max_pages=args.pages)
        calls.insert(0, hot_meta)
        candidates = hot + store_goods
        if not candidates:
            log("没有采集到任何商品，请检查网络或接口可用性")
            return 1

    selected = select_products(candidates, args.target)

    harvested_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    (RAW_DIR / "harvest_all.json").write_text(
        json.dumps(
            {
                "harvested_at": harvested_at,
                "source": "zhaojiafang.com",
                "note": "找家纺网公开前端接口采集的床上用品货源数据，仅用于本项目演示说明。",
                "calls": calls,
                "stores": stores,
                "candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (RAW_DIR / "products_selected.json").write_text(
        json.dumps({"harvested_at": harvested_at, "count": len(selected), "products": selected}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    dist = Counter(g["category"] for g in selected)
    log("=" * 60)
    log(f"候选商品 {len(candidates)} 条 -> 去重选品 {len(selected)} 个 SKU")
    for k, cfg in CATEGORIES.items():
        log(f"  {cfg['label_cn']:<16} {dist.get(k, 0):>3} 个")
    log(f"店铺档案 {len(stores)} 家")
    log(f"原始数据 -> {RAW_DIR / 'harvest_all.json'}")
    log(f"选品结果 -> {RAW_DIR / 'products_selected.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
