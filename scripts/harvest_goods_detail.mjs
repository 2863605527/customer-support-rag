// 从找家纺网采集 60 个选中 SKU 的商品详情（属性/材质/规格/库存/重量）。
//
// 数据来源（m 站 H5 前端接口，无需登录）：
//   GET https://api.zhaojiafang.com/v1/goods/goodsdetailnew/{goods_id}
//   鉴权：authparams 拼进 query + Sign = md5(按 key 排序拼接值 + SignCodeKey)
//   响应：AES-128-CBC(base64)，key 固定（前端 JS 内置），iv 随响应返回
//
// 产出：data/raw/zhaojiafang/goods_details.json（供 build_kb_docs.py 富化知识库文档）
//
// 用法：node scripts/harvest_goods_detail.mjs [--limit N] [--offline]
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { createDecipheriv, createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT_PATH = path.join(ROOT, "data", "raw", "zhaojiafang", "goods_details.json");
const CATALOG_PATH = path.join(ROOT, "data", "raw", "zhaojiafang", "catalog.json");

const API_BASE = "https://api.zhaojiafang.com/v1";
const SIGN_KEY = "H5SIGNKEYZJFk3029ydhsuwei192bmch";
const AES_KEY = "i0sc3UR5FXWiAoJH"; // m 站前端 JS 内置的响应解密密钥
const UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1";
const INTERVAL_MS = 1200; // 限速：礼貌抓取
const MAX_RETRY = 3;

function sign(params) {
  const keys = Object.keys(params)
    .filter((k) => !k.includes(":") && k !== "SignCodeKey" && k !== "Sign")
    .sort();
  let e = "";
  for (const k of keys) e += params[k] === undefined ? "" : String(params[k]);
  return createHash("md5").update(e + SIGN_KEY).digest("hex");
}

function decrypt(payload) {
  const key = Buffer.from(AES_KEY, "utf8");
  const iv = Buffer.from(payload.iv, "utf8");
  const d = createDecipheriv("aes-128-cbc", key, iv);
  let out = d.update(payload.data, "base64", "utf8");
  out += d.final("utf8");
  return JSON.parse(out);
}

async function fetchDetail(goodsId) {
  const params = {
    AppVersion: "3.56",
    Format: "json",
    SystemName: "H5",
    storeid: "1",
    key: "",
    timestamp: String(Date.now()),
    nosign: "nodejs.sign.no",
  };
  params.Sign = sign(params);
  const q = new URLSearchParams(params).toString();
  const url = `${API_BASE}/goods/goodsdetailnew/${goodsId}?${q}`;
  for (let attempt = 1; attempt <= MAX_RETRY; attempt++) {
    try {
      const r = await fetch(url, {
        headers: { "User-Agent": UA, Accept: "application/json", Referer: `https://m.zhaojiafang.com/goods/${goodsId}` },
      });
      const b = await r.json();
      if (b.ResponseStatus !== 0) throw new Error(`ResponseStatus=${b.ResponseStatus} ${b.ResponseMsg || ""}`);
      return decrypt(b.datas);
    } catch (e) {
      console.log(`  [${goodsId}] 第 ${attempt} 次失败：${e.message}`);
      if (attempt === MAX_RETRY) return null;
      await new Promise((r) => setTimeout(r, 1500 * attempt));
    }
  }
  return null;
}

function extract(detail) {
  const attrs = {};
  for (const raw of detail.goods_attributes || []) {
    const [name, value] = String(raw).split("|");
    if (name && value && value !== "请选择") attrs[name.trim()] = value.trim();
  }
  const specs = {};
  for (const s of detail.goods_specs || []) {
    specs[s.spec_title] = (s.spec_values || []).map((v) => v.spec_value);
  }
  return {
    goods_id: detail.goods_id,
    goods_name: detail.goods_name || "",
    goods_subname: detail.goods_subname || "",
    attributes: attrs,
    specs,
    goods_weight_kg: detail.goods_weight,
    storage: detail.goods_storage,
    store_info: detail.store_info?.store_name || detail.store_info?.storeName || "",
  };
}

const args = process.argv.slice(2);
const limitIdx = args.indexOf("--limit");
const LIMIT = limitIdx >= 0 ? Number(args[limitIdx + 1]) : Infinity;

const catalog = JSON.parse(readFileSync(CATALOG_PATH, "utf8"));
const products = catalog.products.slice(0, Number.isFinite(LIMIT) ? LIMIT : undefined);
console.log(`[harvest-detail] 目标 ${products.length} 个 SKU，限速 ${INTERVAL_MS}ms`);

const out = existsSync(OUT_PATH)
  ? JSON.parse(readFileSync(OUT_PATH, "utf8"))
  : { harvested_at: "", items: {} };
let ok = 0;
for (let i = 0; i < products.length; i++) {
  const p = products[i];
  if (out.items[p.goods_id]?.attributes !== undefined) {
    ok++;
    continue; // 幂等：已有数据跳过（删除文件可全量重抓）
  }
  const detail = await fetchDetail(p.goods_id);
  if (detail) {
    out.items[p.goods_id] = extract(detail);
    ok++;
    console.log(`  [${i + 1}/${products.length}] ${p.sku} #${p.goods_id} OK 属性${Object.keys(out.items[p.goods_id].attributes).length}个`);
  } else {
    console.log(`  [${i + 1}/${products.length}] ${p.sku} #${p.goods_id} 失败（跳过）`);
  }
  await new Promise((r) => setTimeout(r, INTERVAL_MS + Math.random() * 600));
}

out.harvested_at = new Date().toISOString();
out.count = Object.keys(out.items).length;
writeFileSync(OUT_PATH, JSON.stringify(out, null, 1), "utf8");
console.log(`[harvest-detail] 完成：成功 ${ok}/${products.length} → ${OUT_PATH}`);
