"""把采集到的货源数据生成为日语知识库文档。

用法::

    python scripts/harvest_zhaojiafang.py          # 1. 采集真实货源数据
    python scripts/build_kb_docs.py                # 2. 生成商品知识库文档
    customer-support ingest                        # 3. 灌入 RAGFlow

产出：
- ``data/knowledge_base/products/KB-XXXX.md``  每个 SKU 一份日语商品文档；
- ``data/knowledge_base/policies/material_guide.md``  素材指南（由采集属性聚合自动生成）；
- ``data/raw/zhaojiafang/catalog.json``        结构化目录（含售价、属性），供评测与复盘；
- 除 material_guide.md 外不触碰 ``policies/`` 下人工撰写的文档。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from customer_support.knowledge.catalog import (  # noqa: E402
    CatalogProduct,
    build_catalog,
    material_value_ja,
    render_product_md,
    season_value_ja,
)

RAW_DIR = ROOT_DIR / "data" / "raw" / "zhaojiafang"
PRODUCTS_DIR = ROOT_DIR / "data" / "knowledge_base" / "products"
GUIDE_PATH = ROOT_DIR / "data" / "knowledge_base" / "policies" / "material_guide.md"


def log(msg: str) -> None:
    print(f"[kb-docs] {msg}", flush=True)


def merge_goods_details(catalog: list[CatalogProduct], details_path: Path) -> int:
    """把 harvest_goods_detail.mjs 采集的详情（属性/规格）按 goods_id 合并进目录。

    文件不存在或个别 SKU 缺失时静默跳过——富化是增量优化，不阻塞文档生成。
    """
    if not details_path.exists():
        log(f"详情采集文件不存在（{details_path.name}），跳过富化；先运行 scripts/harvest_goods_detail.mjs")
        return 0
    payload = json.loads(details_path.read_text(encoding="utf-8"))
    items = payload.get("items") or {}
    enriched = 0
    for product in catalog:
        detail = items.get(str(product.goods_id))
        if not detail:
            continue
        product.material_attrs = detail.get("attributes") or {}
        specs = detail.get("specs") or {}
        product.size_variants = specs.get("尺码") or specs.get("规格") or []
        product.colors = specs.get("颜色") or []
        raw_weight = detail.get("goods_weight_kg")
        try:
            product.weight_kg = float(raw_weight) if raw_weight not in (None, "") else None
        except (TypeError, ValueError):
            product.weight_kg = None
        if product.material_attrs or product.size_variants:
            enriched += 1
    return enriched


def write_material_guide(catalog: list[CatalogProduct]) -> None:
    """聚合全部商品的材质属性，生成一份「素材ガイド」。

    解决「这个商品什么材质」但没带链接/编号的泛问：知识库里有了全店材质构成，
    模型可以给出有依据的通用回答并引导买家提供商品番号，而不是直接转人工。
    """
    value_count: Counter = Counter()
    season_values: Counter = Counter()
    safety_values: Counter = Counter()
    for p in catalog:
        for key, value in p.material_attrs.items():
            if key in ("材质", "面料", "被面材质", "被里材质", "填充物") and value:
                label = material_value_ja(value)
                # 空串 = 「其他」这类占位值，不计入统计
                if label:
                    value_count[label] += 1
            elif key == "适用季节" and value:
                season_values[season_value_ja(value)] += 1
            elif key == "安全类别" and value:
                # 「B类」是中文简体写法，日语写作「B類」
                safety_values[value.replace("类", "類")] += 1

    lines: list[str] = [
        "# 素材ガイド（全店素材構成）",
        "",
        "※本文件由 scripts/build_kb_docs.py 根据货源详情采集数据自动生成，请勿手工编辑。",
        "",
        "本店（くらし寝具）で取り扱う寝具に使われている主な素材の一覧です。",
        "お客様に特定の商品の素材をご案内する際は、商品番号（KB-XXXX）か商品リンクを",
        "ご確認のうえ、各商品ドキュメントの「素材・特徴」セクションを参照してください。",
        "",
        "## 取り扱い素材の一覧",
    ]
    for value, count in value_count.most_common():
        lines.append(f"- {value}：{count} 商品で使用")
    if season_values:
        lines.append("")
        lines.append("## 適用季節の分布")
        for value, count in season_values.most_common():
            lines.append(f"- {value}：{count} 商品")
    if safety_values:
        lines.append("")
        lines.append("## 安全区分（中国国標 GB）")
        for value, count in safety_values.most_common():
            lines.append(f"- {value}：{count} 商品")
        lines.append("")
        lines.append(
            "- A類：乳幼児でも直接肌に触れられる最も厳しい等級、B類：直接肌に触れる製品、"
            "C類：直接肌に触れない製品（中国国家標準 GB 18401 の区分）"
        )
    lines.append("")
    lines.append("## ご案内の注意点")
    lines.append(
        "- 「この商品」とだけ言われた場合は、どの商品か（商品番号・リンク・商品名）を"
        "確認してから具体的な素材をお答えください。"
    )
    lines.append("- 上記の素材は全店のラインナップ全体の傾向です。商品ごとの正式な素材は各商品ドキュメントを参照。")

    GUIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUIDE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"素材ガイド -> {GUIDE_PATH}")


def load_existing_skus(path: Path) -> dict[int, str]:
    """读取上一版目录的「货源 ID → 商品番号」映射，用于扩品时沿用原番号。

    目录不存在或无法解析时返回空映射，行为退回「按顺序重新编号」。
    """
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log(f"已有目录 {path.name} 无法解析，按全新编号生成")
        return {}

    mapping: dict[int, str] = {}
    for item in payload.get("products") or []:
        goods_id, sku = item.get("goods_id"), item.get("sku")
        if goods_id is None or not sku:
            continue
        mapping[int(goods_id)] = str(sku)
    if mapping:
        log(f"沿用已有商品番号 {len(mapping)} 个（扩品不重排）")
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(description="把货源数据生成为日语知识库文档")
    parser.add_argument("--input", type=Path, default=RAW_DIR / "products_selected.json")
    parser.add_argument("--clean", action="store_true", help="生成前清空 products/ 目录（仅删除本脚本生成的 .md）")
    args = parser.parse_args()

    if not args.input.exists():
        log(f"找不到输入文件 {args.input}，请先运行 scripts/harvest_zhaojiafang.py")
        return 2

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    records = payload["products"]
    log(f"读入选品 {len(records)} 条（采集时间 {payload.get('harvested_at')}）")

    existing_skus = load_existing_skus(RAW_DIR / "catalog.json")
    catalog = build_catalog(records, existing_skus=existing_skus)

    # —— 二期富化：合并商品详情采集（材质属性/真实规格 SKU）——
    enriched = merge_goods_details(catalog, RAW_DIR / "goods_details.json")
    log(f"详情富化：{enriched}/{len(catalog)} 个 SKU 带上了货源属性或规格展開")

    PRODUCTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.clean:
        removed = 0
        for old in PRODUCTS_DIR.glob("KB-*.md"):
            old.unlink()
            removed += 1
        log(f"已清理旧商品文档 {removed} 份")

    written = 0
    for product in catalog:
        path = PRODUCTS_DIR / f"{product.sku}.md"
        path.write_text(render_product_md(product), encoding="utf-8")
        written += 1

    write_material_guide(catalog)

    (RAW_DIR / "catalog.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "count": len(catalog),
                "products": [p.to_dict() for p in catalog],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    dist = Counter(p.category_ja for p in catalog)
    prices = [p.price_jpy for p in catalog]
    log("=" * 60)
    log(f"已生成商品文档 {written} 份 -> {PRODUCTS_DIR}")
    for k, v in dist.most_common():
        log(f"  {k:<28} {v:>3} 件")
    log(f"售价区间：{min(prices):,}〜{max(prices):,}円（中央値 {sorted(prices)[len(prices) // 2]:,}円）")
    log(f"结构化目录 -> {RAW_DIR / 'catalog.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
