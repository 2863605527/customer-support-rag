"""商品目录：把采集到的货源记录渲染成可入库的日语商品文档。

为什么要有这一层：

采集回来的是「结构化记录」（中文名 + 批发价 + 店铺），而知识库需要的是
**买家能问得出、模型能引用得到**的文档。中间这层转换如果随手写在脚本里，
就没法单测、也没法复用（以后要生成英文站文档就得重写一遍）。

这里做三件事：
1. 中文名 → 结构化属性（品类 / 素材 / 尺寸 / 件数 / 花型）；
2. 批发价 → 日本站参考零售价（``pricing.py``）；
3. 属性 → 分节落的日语商品文档（按「买家会问什么」组织：価格 / サイズ / 素材 / 手入れ）。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from customer_support.knowledge import localization as L
from customer_support.knowledge.pricing import cost_breakdown, wholesale_to_retail_jpy

# 内部品类标识 -> 日语品类名
CATEGORY_JA: dict[str, str] = {
    "bedding_set": "寝具セット（4点・3点）",
    "quilt": "掛け布団・中綿",
    "pillow": "枕・枕カバー",
    "mattress_pad": "マットレスパッド・敷きパッド",
    "bed_sheet": "シーツ・布団カバー・ベッドスカート",
    "mat": "ござ・涼感マット",
    "blanket": "毛布・ブランケット",
    "accessory": "蚊帳・寝具アクセサリ",
}

# 每种品类的兜底卖点（当素材关键词没命中时使用）
CATEGORY_PITCH: dict[str, str] = {
    "bedding_set": "掛け布団カバー・シーツ・枕カバーがそろったセット。まとめて買い替えたい方に。",
    "quilt": "季節に合わせて選べる掛け布団。軽さと暖かさのバランスを重視した仕様です。",
    "pillow": "首と頭をやさしく支える枕。毎日の睡眠の質を整えます。",
    "mattress_pad": "マットレスの上に敷くだけで、寝心地と通気性が向上します。",
    "bed_sheet": "肌に直接触れるシーツ類。洗い替え用としても人気です。",
    "mat": "夏場のベッドの蒸れを抑える涼感マット。ひんやり快適に過ごせます。",
    "blanket": "肌ざわりのよいブランケット。ソファでのくつろぎ時間にも便利です。",
    "accessory": "寝室環境を快適にする寝具アクセサリです。",
}

# 货源属性名（中文）→ 日语标签；没有映射的键保留中文原文
ATTR_JA: dict[str, str] = {
    "材质": "素材",
    "面料": "生地",
    "支架材质": "フレーム材質",
    "被面材质": "掛けカバー生地",
    "被里材质": "裏地生地",
    "填充物": "中綿（詰め物）",
    "被面成分含量": "表地成分含有率",
    "被里成分含量": "裏地成分含有率",
    "面料支数": "糸番手",
    "克重": "目付",
    "图案": "柄",
    "风格": "スタイル",
    "款式": "デザイン",
    "适用季节": "適用季節",
    "适用对象": "対象",
    "适用年龄": "対象年齢",
    "安全类别": "安全区分",
    "产品等级": "品質等級",
    "是否进口": "輸入の有無",
    "门数量": "開口部数",
    "成分含量": "成分含有率",
    "适用人群": "対象者",
    "适用床尺寸": "対応ベッドサイズ",
}

# 货源材质值（中文）→ 日语常见叫法；没有映射的值保留中文原文
MATERIAL_VALUE_JA: dict[str, str] = {
    "棉": "綿（コットン）",
    "全棉": "綿100%（コットン）",
    "纯棉": "綿100%（コットン）",
    "棉絮": "綿（コットン）",
    "新疆棉": "新疆綿",
    "长绒棉": "超長綿",
    "聚酯纤维": "ポリエステル",
    "涤纶": "ポリエステル",
    "锦纶": "ナイロン",
    "尼龙": "ナイロン",
    "氨纶": "ポリウレタン",
    "丙纶": "ポリプロピレン",
    "粘纤": "レーヨン",
    "人造棉": "レーヨン",
    "醋酸纤维": "アセテート",
    "牛奶绒": "ミルクボア（起毛）",
    "冰丝": "アイスシルク（涼感）",
    "水晶绒": "クリスタルボア",
    "天鹅绒": "ベルベット",
    "大豆纤维": "大豆ファイバー",
    "羊毛": "ウール",
    "羽绒": "ダウン",
    "乳胶": "ラテックス",
    "天然乳胶": "ラテックス",
    "记忆棉": "メモリーフォーム",
    "亚麻": "リネン",
    "蚕丝": "シルク",
    "真丝": "シルク",
    "天丝": "テンセル",
    "珊瑚绒": "珊瑚絨（ボア）",
    "法兰绒": "フランネル",
    "水洗棉": "ウォッシュコットン",
    "针织棉": "ニットコットン",
    "棉麻": "綿麻",
    "竹纤维": "竹繊維",
    "竹炭纤维": "竹炭繊維",
    "雪貂绒": "フェイクファー（起毛）",
    "藤": "籐（ラタン）",
    "凝胶": "ジェル",
    "磨毛": "起毛加工",
    "贡缎": "サテン",
}

# 无信息量的占位值：整段等于这些值时视为「没有材质信息」，不写进文档与统计
_NOISE_MATERIAL_VALUES = frozenset(
    {
        "",
        "-",
        "—",
        "/",
        "none",
        "n/a",
        "na",
        "other",
        "其他",
        "其它",
        "其他/other",
        "无",
        "请选择",
        "暂无",
        "未知",
    }
)

# 货源「适用季节」值（中文）→ 日语；没有映射的值保留原文
SEASON_VALUE_JA: dict[str, str] = {
    "四季通用": "オールシーズン",
    "四季": "オールシーズン",
    "不限季节": "オールシーズン",
    "春季": "春",
    "夏季": "夏",
    "秋季": "秋",
    "冬季": "冬",
    "春夏": "春〜夏",
    "秋冬": "秋〜冬",
    "春秋": "春・秋",
    "春秋冬季": "春・秋・冬",
    "other": "その他",
}


def season_value_ja(value: str) -> str:
    """货源「适用季节」值 → 日语（无映射时保留原文）。"""
    return SEASON_VALUE_JA.get((value or "").strip(), (value or "").strip())


def attr_label_ja(key: str) -> str:
    """货源属性名 → 日语标签（无映射时保留原文，信息不丢失）。"""
    return ATTR_JA.get(key, key)


def is_noise_material(value: str) -> bool:
    """判断材质值是否为「其他」这类占位值（无实际信息）。"""
    return (value or "").strip().lower() in _NOISE_MATERIAL_VALUES


def _translate_material_part(part: str) -> str:
    """翻译单段材质值；无法识别时保留原文。"""
    text = part.strip()
    if not text or is_noise_material(text):
        return ""
    if text in MATERIAL_VALUE_JA:
        return MATERIAL_VALUE_JA[text]

    # 「涤纶(聚酯纤维)」这类「俗称(学名)」写法：括号内外任一侧命中映射即可
    for candidate in re.findall(r"[^()（）]+", text):
        candidate = candidate.strip()
        if candidate in MATERIAL_VALUE_JA:
            return MATERIAL_VALUE_JA[candidate]

    # 「10%大豆纤维」这类带配比的写法：翻译名称并保留配比
    matched = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%\s*(.+)", text)
    if matched:
        name = matched.group(2).strip()
        base = MATERIAL_VALUE_JA.get(name)
        if base:
            return f"{base}{matched.group(1)}%"
    return text


def material_value_ja(value: str) -> str:
    """货源材质值 → 日语常见叫法。

    实际数据里材质值有三种写法，都要处理：

    - 组合值 ``聚酯纤维+10%大豆纤维`` → 按 ``+`` / ``/`` 拆段分别翻译后拼回；
    - 别名写法 ``涤纶(聚酯纤维)`` → 括号内外任一侧命中映射即可；
    - 占位值 ``其他`` / ``other`` → **返回空串**，由调用方过滤掉。

    无法识别的值保留原文，信息不丢失。返回值可能为空串，调用方必须判空。
    """
    parts = [p.strip() for p in re.split(r"[+/、,，]", value or "") if p.strip()]
    return "＋".join(t for t in (_translate_material_part(p) for p in parts) if t)


@dataclass
class CatalogProduct:
    """一个可以直接渲染成知识库文档的商品。"""

    sku: str                       # 店铺内部商品番号，如 KB-0001
    goods_id: int                  # 货源商品 ID（可回溯到原始页面）
    category: str
    category_ja: str
    name_cn: str                   # 原题（内部参考）
    name_ja: str                   # 面向买家的日语商品名（结构化重组）
    name_ja_literal: str           # 逐词翻译版（仅用于检索关键词，不面向买家）
    pattern: str                   # 花型 / 系列名（保留商家原命名）
    price_jpy: int                 # 参考零售价（税込）
    wholesale_cny: float           # 货源批发价（成本参考）
    sizes_cm: list[str] = field(default_factory=list)
    bed_sizes: list[str] = field(default_factory=list)
    specs: list[str] = field(default_factory=list)
    set_pieces: int | None = None
    materials: list[str] = field(default_factory=list)
    pitch: str = ""
    care: list[str] = field(default_factory=list)
    supplier: str = ""             # 仕入先（货源店铺）
    popularity: int = 0            # 货源平台点击量（热度信号）
    stock: int | None = None
    image_url: str = ""
    source_url: str = ""
    tags: list[str] = field(default_factory=list)
    # —— 二期详情采集（harvest_goods_detail.mjs）带来的富化字段 ——
    # 货源属性（商品详情页 goods_attributes，中文原值，如 材质=棉）
    material_attrs: dict[str, str] = field(default_factory=dict)
    # 货源规格展開（颜色/尺码等真实 SKU 选项）
    size_variants: list[str] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    weight_kg: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def build_product(record: dict, sku: str) -> CatalogProduct:
    """把一条采集记录转成 CatalogProduct。"""
    cn_name: str = record["goods_name"]
    category = record.get("category", "")

    title = L.compose_title(cn_name)
    materials = L.detect_materials(cn_name)
    categories = L.detect_categories(cn_name)
    # 卖点与保养建议按「识别到的全部素材/品类」判断，比只匹配标题更准
    signals = " ".join(materials + categories) or title

    sizes_cm, bed_sizes = L.extract_sizes(cn_name)
    # 卖点优先按素材命中，其次按品类兜底——品类级兜底比通用文案对买家有用
    pitch = L.material_pitch(signals) or CATEGORY_PITCH.get(
        category, "毎日の寝心地を整えるベーシックな寝具です。"
    )
    return CatalogProduct(
        sku=sku,
        goods_id=int(record["goods_id"]),
        category=category,
        category_ja=CATEGORY_JA.get(category, category or "寝具"),
        name_cn=cn_name,
        name_ja=title,
        name_ja_literal=L.localize_name(cn_name),
        pattern=L.extract_pattern(cn_name),
        price_jpy=wholesale_to_retail_jpy(float(record["price_cny_wholesale"])),
        wholesale_cny=float(record["price_cny_wholesale"]),
        sizes_cm=sizes_cm,
        bed_sizes=bed_sizes,
        specs=L.extract_specs(cn_name),
        set_pieces=L.extract_set_pieces(cn_name),
        materials=materials,
        pitch=pitch,
        care=L.care_instructions(signals),
        supplier=record.get("store_name", "") or "",
        popularity=int(record.get("clicks") or 0),
        stock=record.get("stock"),
        image_url=record.get("image_url", ""),
        source_url=record.get("detail_url", ""),
        tags=[t.get("tagname", "") for t in (record.get("tags") or []) if isinstance(t, dict)],
    )


def build_catalog(
    records: list[dict],
    sku_prefix: str = "KB",
    existing_skus: dict[int, str] | None = None,
) -> list[CatalogProduct]:
    """按品类分组排序后编号，生成完整目录。

    ``existing_skus`` 传入「货源 ID → 已有商品番号」映射时启用**稳定编号**：
    已在库的商品沿用原番号，新商品接在最大番号之后。

    为什么需要它：番号来自「排序后的序号」，而排序按品类分组。选品规模一变
    （例如 60 → 120），每个品类的商品数都变，品类间的分界点随之平移，
    于是**所有旧番号都会指向别的商品**——既有文档、catalog 里的链接锚定
    和 RAGFlow 索引会整体失效，必须全量重解析。沿用原番号后，扩品只产生
    新增文档，老文档内容不变。
    """
    ordered = sorted(records, key=lambda r: (r.get("category", ""), -(r.get("clicks") or 0)))

    if not existing_skus:
        return [build_product(r, f"{sku_prefix}-{i:04d}") for i, r in enumerate(ordered, 1)]

    pattern = re.compile(rf"{re.escape(sku_prefix)}-(\d+)$")
    next_no = 1
    # 不回收历史番号：即使某个商品本次未被选中，它的番号也留空不复用，
    # 否则「KB-0042」在不同时间会指向不同商品，对账与工单引用都会错乱。
    for existing in existing_skus.values():
        matched = pattern.match(existing)
        if matched:
            next_no = max(next_no, int(matched.group(1)) + 1)

    products: list[CatalogProduct] = []
    for record in ordered:
        goods_id = int(record.get("goods_id") or 0)
        sku = existing_skus.get(goods_id)
        if not sku:
            sku = f"{sku_prefix}-{next_no:04d}"
            next_no += 1
        products.append(build_product(record, sku))
    return products


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _yen(amount: int) -> str:
    return f"{amount:,}円"


def render_product_md(p: CatalogProduct) -> str:
    """渲染一份商品知识库文档（Markdown）。

    结构按「买家会问什么」组织：価格 / セット内容 / サイズ / 素材 / 手入れ / 注意。
    末尾附「社内参考情報」，让客服核对货源与成本时不必再打开外部页面。
    """
    lines: list[str] = []
    title = p.name_ja + (f"／{p.pattern}" if p.pattern else "")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"商品番号：**{p.sku}**　ジャンル：{p.category_ja}")
    lines.append("")

    # —— 価格 ——
    lines.append("## 価格")
    lines.append(f"- 販売価格：**{_yen(p.price_jpy)}**（税込）")
    lines.append("- 表示価格は税込です。セール期間中は別途割引が適用される場合があります。")
    lines.append("")

    # —— セット内容 ——
    if p.set_pieces:
        contents = {
            4: "掛け布団カバー×1、シーツ×1（フラットまたはボックス）、枕カバー×2",
            3: "掛け布団カバー×1、シーツ×1、枕カバー×1",
        }[p.set_pieces]
        lines.append("## セット内容")
        lines.append(f"- {p.set_pieces}点セット：{contents}")
        lines.append("- ※セット内容は仕様（床単款／床笠款など）により異なる場合があります。")
        lines.append("")

    # —— サイズ ——
    lines.append("## サイズ")
    if p.sizes_cm:
        lines.append(f"- 展開サイズ：{'、'.join(p.sizes_cm)}")
    if p.bed_sizes:
        lines.append(f"- 対応ベッド幅：{'、'.join(p.bed_sizes)}")
    if p.specs:
        lines.append(f"- 仕様：{'、'.join(p.specs)}")
    if p.size_variants:
        # 货源详情页的真实尺码 SKU（中文原文，含精确尺寸，如 单床笠120*200）
        lines.append(f"- サイズ展開（全{len(p.size_variants)}種）：{'、'.join(p.size_variants)}")
    if not (p.sizes_cm or p.bed_sizes or p.specs or p.size_variants):
        lines.append("- サイズ展開は商品ページの仕様をご確認ください。")
    lines.append("- 商品名に記載のないサイズのお取り扱いはございません。")
    lines.append("")

    # —— 素材・特徴 ——
    lines.append("## 素材・特徴")
    lines.append(f"- {p.pitch}")
    if p.materials:
        lines.append(f"- 素材キーワード：{'、'.join(dict.fromkeys(p.materials))}")
    if p.material_attrs:
        # 货源详情页的商品属性（真实材质成分），客服回答材质问题的第一依据。
        # 翻译后为空的是「其他」这类占位值，直接丢掉，免得读者在无信息项上花时间。
        parts = [
            f"{attr_label_ja(k)}={label}"
            for k, v in sorted(p.material_attrs.items())
            if (label := material_value_ja(v))
        ]
        if parts:
            lines.append(f"- 貨源属性（正式値）：{'；'.join(parts)}")
    if p.weight_kg:
        lines.append(f"- 商品重量：約{p.weight_kg:g}kg")
    if p.colors:
        shown = "、".join(p.colors[:12]) + ("…" if len(p.colors) > 12 else "")
        lines.append(f"- カラー展開：{shown}")
    if p.tags:
        lines.append(f"- タグ：{'、'.join(sorted(set(p.tags)))}")
    lines.append("")

    # —— お手入れ ——
    lines.append("## お手入れ方法")
    for c in p.care:
        lines.append(f"- {c}")
    lines.append("")

    # —— ご注意 ——
    lines.append("## ご注意")
    lines.append("- モニター環境により実際の色味と多少異なって見える場合があります。")
    lines.append("- プリント柄・刺繍商品は、ロットにより柄の出方が多少異なる場合があります。")
    lines.append("- 品質に問題がある場合は、到着後30日以内にカスタマーサポートへご連絡ください。")
    lines.append("")

    # —— 検索用キーワード（提升跨语言召回，不面向买家展示）——
    lines.append("## 検索キーワード（社内用）")
    keyword_bits = [p.name_ja_literal, p.name_cn]
    if p.pattern:
        keyword_bits.append(p.pattern)
    if p.material_attrs:
        # 中文材质原值进检索词：买家中文提问（棉/聚酯纤维）也能召回对应商品
        keyword_bits.extend(v for v in p.material_attrs.values() if v and not is_noise_material(v))
    lines.append("- " + " / ".join(b for b in keyword_bits if b))
    lines.append("")

    # —— 社内参考 ——
    lines.append("## 社内参考情報")
    lines.append(f"- 原題（中国語）：{p.name_cn}")
    lines.append(f"- 仕入先：{p.supplier or '—'}")
    lines.append(f"- 貨源番号：{p.goods_id}（{p.source_url}）")
    breakdown = cost_breakdown(p.wholesale_cny)
    lines.append(
        f"- 仕入価格（参考）：{p.wholesale_cny:.2f}元 → 国内段コスト {breakdown['domestic_cost_cny']:.2f}元"
        f" → 販売価格 {_yen(p.price_jpy)}（為替レート・加価率は社内規定）"
    )
    lines.append(f"- 話題性指標：貨源サイトのクリック数 {p.popularity:,}")
    return "\n".join(lines) + "\n"
