# 知识库目录说明

本目录是客服 RAG 的知识源，**按业务分文件夹组织**（加载器会递归扫描）：

```
data/knowledge_base/
├── products/                 # 商品文档（脚本生成，不要手改）
│   ├── KB-0001.md … KB-0120.md
├── policies/                 # 店铺政策与售后规则（人工撰写，日语）
│   ├── after_sales_policy.md        返品・交換ポリシー
│   ├── shipping_policy.md           配送について
│   ├── payment_and_tax.md           お支払い・関税
│   ├── quality_warranty.md          品質保証・検品
│   ├── faq.md                       よくあるご質問
│   ├── care_and_washing.md          お手入れ・洗濯ガイド
│   ├── bedding_materials.md         素材の種類と安全性
│   ├── customs_and_parcel_issues.md 通関・配送トラブル時の対応
│   ├── order_changes.md             ご注文後の変更・キャンセル
│   └── material_guide.md            素材ガイド（脚本生成）
└── store/                    # 店铺与选购辅助（人工撰写，日语）
    ├── store_profile.md        店舗情報・サポート窓口
    ├── size_guide.md           寝具サイズ早見表
    └── bedding_selection.md    季節・用途別の寝具選び
```

## 约定

- 文件名以 `_` 或 `.` 开头 = 草稿/隐藏，**不进知识库**；
- `README.md`、`CHANGELOG.md`、`LICENSE` 这类说明文件会被加载器自动跳过；
- 支持 `.md` / `.txt` / `.pdf`；
- 文档以**日语**为主：买家用日语提问，日语文档的向量匹配度最高，
  跨语言检索由多语言向量模型承担（中文提问同样能命中）。

## 商品文档怎么来的

`products/` 下的 120 份文档**不是手写的**，由脚本从真实货源数据生成：

```bash
# 1. 从找家纺网公开接口采集真实床上用品货源数据（--target 指定 SKU 数）
python scripts/harvest_zhaojiafang.py --target 120

# 2. 采集商品详情（材质属性 / 规格展開 / 重量），幂等增量
node scripts/harvest_goods_detail.mjs

# 3. 生成日语商品文档 + 结构化目录
python scripts/build_kb_docs.py
```

### 扩品时的番号稳定性

商品番号（`KB-XXXX`）沿用**上一版 `catalog.json` 已分配的原番号**，新增商品接在
最大番号之后。因此把 `--target` 从 60 提到 120 时：

- 原有 60 个番号**一个都不会变**（否则它们会指向别的商品）；
- 新增商品从 `KB-0061` 起连续编号；
- 既有文档内容不变，入库时只需增量解析新增部分。

番号一旦分配就不回收。某个商品本次未入选时，它的番号留空不复用——
否则同一个 `KB-0042` 在不同时间会指向不同商品，对账与工单引用都会错乱。
删掉 `catalog.json` 再生成会退回「按顺序重新编号」的行为。

数据流：

```
找家纺网公开接口
  → data/raw/zhaojiafang/harvest_all.json       全量候选（含调用记录，可审计）
  → data/raw/zhaojiafang/products_selected.json 选中的 SKU（当前 120 个）
  → data/raw/zhaojiafang/catalog.json           结构化目录（含日本站售价）
  → data/knowledge_base/products/KB-XXXX.md     日语商品文档（入库单元）
```

真实数据字段：商品名、批发价、点击量、库存、货源店铺、商品图、商品页链接。
日本站售价是基于「批发价 → 国内段成本 → 汇率 → 跨境加价」规则的**派生值**，
规则见 `src/customer_support/knowledge/pricing.py`，文档里也标注了成本口径。

> 商品数据来自 zhaojiafang.com 的公开前端接口，属于临时取数方式；
> 正式使用应取得授权或签署数据合作协议，并遵守对方 robots 与使用条款。

## 入库

```bash
customer-support ingest            # 增量入库（同名跳过）
customer-support ingest --replace  # 覆盖同名文档
customer-support ingest --prune    # 同时清理远端多余的文档（会先备份）
```

## Milvus 后端

若把 `RETRIEVAL_BACKEND` 切到 `milvus`，本项目会自行读取本目录并切片入库：

```bash
customer-support build-kb
```
