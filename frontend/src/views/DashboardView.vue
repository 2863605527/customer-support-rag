<script setup lang="ts">
/**
 * 运营仪表盘：量化客服系统的实际效果。
 *
 * 核心指标是 **自助解决率（containment rate）**——没有转人工的回答占比。它直接对应
 * 「AI 替人挡掉了多少工作量」；其余指标（转人工原因分布、分品类反馈、延迟、熔断状态）
 * 用于定位「为什么会这样、下一步该改哪里」。
 *
 * 图表用内联 SVG 手绘，不引入图表库：数据只有几张卡片和一个按天趋势，零依赖更可控。
 */
import { computed, onMounted, ref } from "vue";
import { consoleApi, humanizeError } from "@/api/client";
import type { AnalyticsOverview } from "@/types";

const days = ref(7);
const data = ref<AnalyticsOverview | null>(null);
const loading = ref(false);
const err = ref("");

const REASON_LABEL: Record<string, string> = {
  high_risk_intent: "高风险诉求",
  low_confidence: "意图不确定",
  no_knowledge: "知识库无资料",
  service_unavailable: "依赖不可用",
  system_error: "系统异常",
};
const SOURCE_LABEL: Record<string, string> = {
  knowledge: "知识库作答",
  no_answer: "资料不足",
  human: "转人工",
  unknown: "未知",
};

/** 差评原因标签（与后端 models/feedback.py 的 DOWN_REASONS 对应）。 */
const DOWN_REASON_LABEL: Record<string, string> = {
  wrong_answer: "答错了",
  missing_info: "信息不全",
  missing_product: "缺该商品资料",
  not_relevant: "答非所问",
  wrong_language: "语言不对",
  too_generic: "太笼统",
  out_of_scope: "超出范围",
  other: "其他",
};

function label(map: Record<string, string>, key: string): string {
  return map[key] ?? key;
}

const conv = computed(() => data.value?.conversation ?? null);

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

async function load(): Promise<void> {
  loading.value = true;
  err.value = "";
  try {
    data.value = await consoleApi.analytics(days.value);
  } catch (e) {
    err.value = humanizeError(e);
  } finally {
    loading.value = false;
  }
}

// ---- 趋势图：按天画「提问 / 作答 / 转人工」三色柱 ----
const CHART_H = 120;
const CHART_W = 640;
const PAD_L = 26;
const PAD_B = 20;

const daily = computed(() => conv.value?.daily ?? []);
const chartMax = computed(() => Math.max(1, ...daily.value.map((d) => d.questions)));

const bars = computed(() => {
  const n = daily.value.length || 1;
  const plotW = CHART_W - PAD_L - 8;
  const slot = plotW / n;
  const barW = Math.max(3, slot / 4.2);
  return daily.value.map((d, i) => {
    const x0 = PAD_L + i * slot + slot / 2;
    const scale = (v: number): number => (v / chartMax.value) * (CHART_H - PAD_B - 8);
    const y = (v: number): number => CHART_H - PAD_B - scale(v);
    return {
      day: d.day.slice(5), // MM-DD
      questions: { x: x0 - barW * 1.5, y: y(d.questions), h: scale(d.questions) },
      answers: { x: x0 - barW / 2, y: y(d.answers), h: scale(d.answers) },
      handoffs: { x: x0 + barW / 2, y: y(d.handoffs), h: scale(d.handoffs) },
      label: d.day.slice(5),
    };
  });
});

/** 排序后的分布项（用于 by_intent / by_source 等列表）。 */
function sorted(rec: Record<string, number> | undefined): [string, number][] {
  if (!rec) return [];
  return Object.entries(rec).sort((a, b) => b[1] - a[1]);
}

function barWidth(v: number, all: [string, number][]): string {
  const max = Math.max(1, ...all.map(([, n]) => n));
  return `${Math.round((v / max) * 100)}%`;
}

onMounted(load);
</script>

<template>
  <div class="dash">
    <header class="head">
      <h1>运营仪表盘</h1>
      <div class="actions">
        <select v-model.number="days" @change="load">
          <option :value="1">最近 24 小时</option>
          <option :value="7">最近 7 天</option>
          <option :value="30">最近 30 天</option>
          <option :value="90">最近 90 天</option>
        </select>
        <button class="ghost" :disabled="loading" @click="load">{{ loading ? "加载中…" : "刷新" }}</button>
      </div>
    </header>

    <p v-if="err" class="err">{{ err }}</p>

    <template v-if="conv">
      <!-- KPI -->
      <div class="kpis">
        <div class="kpi hero">
          <span class="k">自助解决率</span>
          <b>{{ pct(conv.containment_rate) }}</b>
          <span class="sub">没有转人工的回答占比 · 越高越省人力</span>
        </div>
        <div class="kpi"><span class="k">提问数</span><b>{{ conv.questions }}</b><span class="sub">买家消息</span></div>
        <div class="kpi"><span class="k">回答数</span><b>{{ conv.answers }}</b><span class="sub">客服回复</span></div>
        <div class="kpi"><span class="k">转人工</span><b class="warn">{{ conv.handoffs }}</b><span class="sub">{{ pct(conv.handoff_rate) }}</span></div>
        <div class="kpi"><span class="k">会话数</span><b>{{ conv.conversations }}</b><span class="sub">独立会话</span></div>
        <div class="kpi"><span class="k">平均延迟</span><b>{{ conv.avg_latency_ms }}<i>ms</i></b><span class="sub">缓存命中不计入</span></div>
        <div class="kpi">
          <span class="k">回答好评率</span>
          <b>{{ pct(data?.feedback.positive_rate) }}</b>
          <span class="sub">👍 {{ data?.feedback.up ?? 0 }} / 👎 {{ data?.feedback.down ?? 0 }}</span>
        </div>
        <div class="kpi"><span class="k">缓存命中</span><b>{{ data?.cache.hits ?? 0 }}</b><span class="sub">条目 {{ data?.cache.entries ?? 0 }}</span></div>
      </div>

      <!-- 趋势 -->
      <section class="panel">
        <h2>每日趋势（提问 / 作答 / 转人工）</h2>
        <svg v-if="daily.length" class="chart" :viewBox="`0 0 ${CHART_W} ${CHART_H}`" role="img">
          <line :x1="PAD_L" :y1="CHART_H - PAD_B" :x2="CHART_W - 8" :y2="CHART_H - PAD_B" class="axis" />
          <g v-for="(b, i) in bars" :key="i">
            <rect :x="b.questions.x" :y="b.questions.y" :width="6" :height="b.questions.h" class="bar-q" />
            <rect :x="b.answers.x" :y="b.answers.y" :width="6" :height="b.answers.h" class="bar-a" />
            <rect :x="b.handoffs.x" :y="b.handoffs.y" :width="6" :height="b.handoffs.h" class="bar-h" />
            <text :x="b.questions.x + 9" :y="CHART_H - 6" class="xlab">{{ b.label }}</text>
          </g>
        </svg>
        <p v-else class="dim small">所选区间内还没有数据。</p>
        <div class="legend">
          <span><i class="sw q" />提问</span>
          <span><i class="sw a" />作答</span>
          <span><i class="sw h" />转人工</span>
        </div>
      </section>

      <!-- 分布 -->
      <div class="grid3">
        <section class="panel">
          <h2>回答来源</h2>
          <ul class="dist">
            <li v-for="[k, v] in sorted(conv.by_source)" :key="k">
              <span class="name">{{ label(SOURCE_LABEL, k) }}</span>
              <span class="bar"><i :style="{ width: barWidth(v, sorted(conv.by_source)) }" /></span>
              <span class="num">{{ v }}</span>
            </li>
            <li v-if="!sorted(conv.by_source).length" class="dim small">暂无数据</li>
          </ul>
        </section>

        <section class="panel">
          <h2>意图分布</h2>
          <ul class="dist">
            <li v-for="[k, v] in sorted(conv.by_intent)" :key="k">
              <span class="name">{{ k }}</span>
              <span class="bar"><i :style="{ width: barWidth(v, sorted(conv.by_intent)) }" /></span>
              <span class="num">{{ v }}</span>
            </li>
            <li v-if="!sorted(conv.by_intent).length" class="dim small">暂无数据</li>
          </ul>
        </section>

        <section class="panel">
          <h2>转人工原因</h2>
          <ul class="dist">
            <li v-for="[k, v] in sorted(conv.by_handoff_reason)" :key="k">
              <span class="name">{{ label(REASON_LABEL, k) }}</span>
              <span class="bar"><i :style="{ width: barWidth(v, sorted(conv.by_handoff_reason)) }" /></span>
              <span class="num">{{ v }}</span>
            </li>
            <li v-if="!sorted(conv.by_handoff_reason).length" class="dim small">暂无转人工</li>
          </ul>
        </section>
      </div>

      <!-- 工单 + 反馈 + 依赖健康 -->
      <div class="grid3">
        <section class="panel">
          <h2>工单积压（近 7 天）</h2>
          <ul class="kv">
            <li><span>工单总数</span><b>{{ data?.ticket.total ?? 0 }}</b></li>
            <li><span>待处理积压</span><b class="warn">{{ data?.ticket.backlog ?? 0 }}</b></li>
            <li><span>超 24h 未处理</span><b class="err">{{ data?.ticket.overdue ?? 0 }}</b></li>
            <li><span>未指派</span><b>{{ data?.ticket.unassigned ?? 0 }}</b></li>
            <li>
              <span>平均首次响应</span>
              <b>{{ data?.ticket.avg_first_response_minutes != null ? data.ticket.avg_first_response_minutes + " 分" : "—" }}</b>
            </li>
            <li>
              <span>平均解决时长</span>
              <b>{{ data?.ticket.avg_resolve_minutes != null ? data.ticket.avg_resolve_minutes + " 分" : "—" }}</b>
            </li>
          </ul>
        </section>

        <section class="panel">
          <h2>差评原因 TOP</h2>
          <ul class="dist">
            <li v-for="[k, v] in sorted(data?.feedback.by_reason)" :key="k">
              <span class="name">{{ label(DOWN_REASON_LABEL, k) }}</span>
              <span class="bar"><i :style="{ width: barWidth(v, sorted(data?.feedback.by_reason)) }" /></span>
              <span class="num">{{ v }}</span>
            </li>
            <li v-if="!sorted(data?.feedback.by_reason).length" class="dim small">暂无差评 —— 太好了</li>
          </ul>
        </section>

        <section class="panel">
          <h2>依赖健康</h2>
          <ul class="kv">
            <li>
              <span>LLM 可用</span>
              <span :class="['badge', data?.llm?.ok ? 'ok' : 'err']">
                {{ data?.llm?.ok ? "正常" : "异常" }}
              </span>
            </li>
            <li v-if="data?.llm?.quota_exhausted">
              <span>账户状态</span><span class="badge err">余额/配额不足</span>
            </li>
            <li><span>LLM 累计调用</span><b>{{ data?.llm?.total_calls ?? 0 }}</b></li>
            <li><span>LLM 失败次数</span><b :class="{ err: (data?.llm?.total_failures ?? 0) > 0 }">{{ data?.llm?.total_failures ?? 0 }}</b></li>
          </ul>
          <ul class="dist" v-if="data?.breakers.length">
            <li v-for="b in data.breakers" :key="b.name">
              <span class="name">{{ b.name }}</span>
              <span :class="['badge', b.state === 'closed' ? 'ok' : b.state === 'half_open' ? 'warn' : 'err']">
                {{ b.state === "closed" ? "闭合" : b.state === "half_open" ? "半开" : "熔断" }}
              </span>
              <span class="num dim">失败 {{ b.failures }}</span>
            </li>
          </ul>
        </section>
      </div>

      <!-- 评测集导出入口（提示，实际在工单/反馈接口） -->
      <p class="foot dim small">
        想把这些差评变成回归用例？管理员可调用 <code>/api/v1/ops/feedback/dataset</code> 导出 JSONL 评测集，
        用于下次改 prompt / 调阈值后的回归对比。
      </p>
    </template>
    <p v-else-if="!loading && !err" class="dim">正在加载指标…</p>
  </div>
</template>

<style scoped>
.dash { flex: 1; min-width: 0; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 14px; padding-right: 4px; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.head h1 { margin: 0; font-size: 18px; }
.actions { display: flex; gap: 8px; }
.actions select, .actions .ghost {
  border: 1px solid var(--c-border); border-radius: 8px; padding: 6px 12px;
  background: var(--c-panel); font-size: 12px;
}
.actions .ghost:hover { border-color: var(--c-primary); color: var(--c-primary); }

.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.kpi {
  background: var(--c-panel); border: 1px solid var(--c-border); border-radius: 10px;
  padding: 10px 14px; display: flex; flex-direction: column; gap: 2px;
}
.kpi.hero { border-color: var(--c-primary); background: linear-gradient(180deg, var(--c-primary-soft), var(--c-panel)); }
.kpi .k { font-size: 11px; color: var(--c-text-dim); }
.kpi b { font-size: 22px; line-height: 1.2; }
.kpi b.warn { color: var(--c-warn); }
.kpi b i { font-size: 12px; font-style: normal; color: var(--c-text-dim); margin-left: 2px; }
.kpi .sub { font-size: 11px; color: var(--c-text-dim); }

.panel {
  background: var(--c-panel); border: 1px solid var(--c-border); border-radius: var(--radius);
  padding: 14px 16px;
}
.panel h2 { margin: 0 0 10px; font-size: 13px; color: var(--c-text-dim); font-weight: 600; }

.chart { width: 100%; height: 150px; }
.axis { stroke: var(--c-border); stroke-width: 1; }
.bar-q { fill: #2456e6; }
.bar-a { fill: #178a50; }
.bar-h { fill: #c46a00; }
.xlab { font-size: 8px; fill: #6b7684; }
.legend { display: flex; gap: 16px; margin-top: 6px; font-size: 11px; color: var(--c-text-dim); }
.legend .sw { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 4px; }
.legend .sw.q { background: #2456e6; }
.legend .sw.a { background: #178a50; }
.legend .sw.h { background: #c46a00; }

.grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; align-items: start; }

.dist { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.dist li { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.dist .name { flex: none; width: 92px; color: var(--c-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dist .bar { flex: 1; height: 8px; border-radius: 999px; background: #eef0f3; overflow: hidden; }
.dist .bar i { display: block; height: 100%; background: var(--c-primary); border-radius: 999px; }
.dist .num { flex: none; min-width: 26px; text-align: right; color: var(--c-text-dim); }

.kv { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.kv li { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; }
.kv li > span:first-child { color: var(--c-text-dim); }
.kv b { font-size: 14px; }
.kv b.warn { color: var(--c-warn); }
.kv b.err { color: var(--c-err); }

.dim { color: var(--c-text-dim); }
.small { font-size: 12px; }
.err { color: var(--c-err); font-size: 13px; }
.foot { margin: 0; line-height: 1.8; }
.foot code { background: #eef0f3; padding: 1px 5px; border-radius: 4px; font-size: 11px; }

@media (max-width: 1100px) {
  .kpis { grid-template-columns: repeat(2, 1fr); }
  .grid3 { grid-template-columns: 1fr; }
}
</style>
