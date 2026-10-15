<script setup lang="ts">
/**
 * 工单中心：把「AI 转人工」产生的工单真正走完闭环。
 *
 * 为什么这是刚需：早先工单只是 JSONL 里的一行记录——**只记录不处理等于没有工单**。
 * 这一页提供筛选、指派、状态流转、人工回复与 SLA 看板，值班同事能在这里把单子清零。
 *
 * 权限：客服可查看与筛选；状态流转 / 指派 / 回复需要管理员（与后端 ``require_admin`` 一致）。
 */
import { computed, onMounted, ref } from "vue";
import { consoleApi, humanizeError } from "@/api/client";
import { useAuth } from "@/composables/useAuth";
import type { Ticket, TicketPriority, TicketStats, TicketStatus } from "@/types";

const { currentUser } = useAuth();
const isAdmin = computed(() => currentUser.value?.role === "admin");

const STATUS_LABEL: Record<TicketStatus, string> = {
  open: "待处理",
  in_progress: "处理中",
  resolved: "已解决",
  closed: "已关闭",
};
const STATUS_CLASS: Record<TicketStatus, string> = {
  open: "warn",
  in_progress: "",
  resolved: "ok",
  closed: "dim",
};
const PRIORITY_LABEL: Record<TicketPriority, string> = {
  low: "低",
  normal: "普通",
  high: "高",
  urgent: "紧急",
};
const PRIORITY_CLASS: Record<TicketPriority, string> = {
  low: "dim",
  normal: "",
  high: "warn",
  urgent: "err",
};
// 转人工原因：与后端 chat_service 的 reason 取值一一对应
const REASON_LABEL: Record<string, string> = {
  high_risk_intent: "高风险诉求",
  low_confidence: "意图不确定",
  no_knowledge: "知识库无资料",
  service_unavailable: "依赖不可用",
  system_error: "系统异常",
};

function reasonLabel(r: string): string {
  return REASON_LABEL[r] ?? r;
}

// ---- 列表 ----
const items = ref<Ticket[]>([]);
const total = ref(0);
const stats = ref<TicketStats | null>(null);
const loading = ref(false);
const err = ref("");

const filterStatus = ref<string>("");
const filterPriority = ref<string>("");
const filterReason = ref<string>("");
const search = ref("");

async function load(): Promise<void> {
  loading.value = true;
  err.value = "";
  try {
    const page = await consoleApi.tickets({
      status: filterStatus.value || undefined,
      priority: filterPriority.value || undefined,
      reason: filterReason.value || undefined,
      search: search.value.trim() || undefined,
      limit: 50,
    });
    items.value = page.items;
    total.value = page.total;
  } catch (e) {
    err.value = humanizeError(e);
  } finally {
    loading.value = false;
  }
}

async function loadStats(): Promise<void> {
  try {
    stats.value = await consoleApi.ticketStats(7);
  } catch {
    stats.value = null;
  }
}

function resetFilters(): void {
  filterStatus.value = filterPriority.value = filterReason.value = search.value = "";
  void load();
}

// ---- 详情与操作 ----
const selected = ref<Ticket | null>(null);
const replyBody = ref("");
const replyInternal = ref(false);
const saving = ref(false);
const actionErr = ref("");

function open(ticket: Ticket): void {
  selected.value = ticket;
  replyBody.value = "";
  replyInternal.value = false;
  actionErr.value = "";
}

/** 局部更新列表里的那一条，避免整表重载造成闪烁。 */
function replaceRow(updated: Ticket): void {
  items.value = items.value.map((t) => (t.ticket_id === updated.ticket_id ? updated : t));
  selected.value = updated;
}

async function patch(payload: { status?: TicketStatus; priority?: TicketPriority; assignee?: string }): Promise<void> {
  const target = selected.value;
  if (!target || saving.value) return;
  saving.value = true;
  actionErr.value = "";
  try {
    replaceRow(await consoleApi.updateTicket(target.ticket_id, payload));
    void loadStats();
  } catch (e) {
    actionErr.value = humanizeError(e);
  } finally {
    saving.value = false;
  }
}

async function claimSelf(): Promise<void> {
  await patch({ assignee: currentUser.value?.email ?? "" });
}

async function sendReply(): Promise<void> {
  const target = selected.value;
  const body = replyBody.value.trim();
  if (!target || !body || saving.value) return;
  saving.value = true;
  actionErr.value = "";
  try {
    replaceRow(await consoleApi.replyTicket(target.ticket_id, body, replyInternal.value));
    replyBody.value = "";
    replyInternal.value = false;
    void loadStats();
  } catch (e) {
    actionErr.value = humanizeError(e);
  } finally {
    saving.value = false;
  }
}

function fmt(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

onMounted(() => {
  void load();
  void loadStats();
});
</script>

<template>
  <div class="tickets">
    <!-- 左：看板 + 列表 -->
    <section class="list-col">
      <div class="cards">
        <div class="card"><span class="k">近 7 天工单</span><b>{{ stats?.total ?? "—" }}</b></div>
        <div class="card"><span class="k">待处理积压</span><b class="warn">{{ stats?.backlog ?? "—" }}</b></div>
        <div class="card"><span class="k">超 24h 未处理</span><b class="err">{{ stats?.overdue ?? "—" }}</b></div>
        <div class="card"><span class="k">未指派</span><b>{{ stats?.unassigned ?? "—" }}</b></div>
        <div class="card">
          <span class="k">平均首次响应</span>
          <b>{{ stats?.avg_first_response_minutes != null ? stats.avg_first_response_minutes + " 分" : "—" }}</b>
        </div>
        <div class="card">
          <span class="k">平均解决时长</span>
          <b>{{ stats?.avg_resolve_minutes != null ? stats.avg_resolve_minutes + " 分" : "—" }}</b>
        </div>
      </div>

      <div class="filters">
        <select v-model="filterStatus" @change="load">
          <option value="">全部状态</option>
          <option value="open">待处理</option>
          <option value="in_progress">处理中</option>
          <option value="resolved">已解决</option>
          <option value="closed">已关闭</option>
        </select>
        <select v-model="filterPriority" @change="load">
          <option value="">全部优先级</option>
          <option value="urgent">紧急</option>
          <option value="high">高</option>
          <option value="normal">普通</option>
          <option value="low">低</option>
        </select>
        <select v-model="filterReason" @change="load">
          <option value="">全部原因</option>
          <option v-for="(label, code) in REASON_LABEL" :key="code" :value="code">{{ label }}</option>
        </select>
        <input v-model="search" placeholder="搜索问题 / 工单号" @keyup.enter="load" />
        <button class="ghost" @click="load">查询</button>
        <button class="ghost" @click="resetFilters">重置</button>
      </div>

      <p v-if="err" class="err">{{ err }}</p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>工单号</th><th>问题</th><th>原因</th><th>优先级</th><th>状态</th><th>处理人</th><th>创建</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="t in items"
              :key="t.ticket_id"
              :class="{ on: selected?.ticket_id === t.ticket_id }"
              @click="open(t)"
            >
              <td class="mono">{{ t.ticket_id }}</td>
              <td class="q">{{ t.question }}</td>
              <td><span class="badge dim">{{ reasonLabel(t.reason) }}</span></td>
              <td><span :class="['badge', PRIORITY_CLASS[t.priority]]">{{ PRIORITY_LABEL[t.priority] }}</span></td>
              <td><span :class="['badge', STATUS_CLASS[t.status]]">{{ STATUS_LABEL[t.status] }}</span></td>
              <td class="dim">{{ t.assignee || "未指派" }}</td>
              <td class="mono dim">{{ new Date(t.created_at).toLocaleDateString() }}</td>
            </tr>
            <tr v-if="!items.length && !loading"><td colspan="7" class="dim empty">没有符合条件的工单</td></tr>
            <tr v-if="loading"><td colspan="7" class="dim empty">加载中…</td></tr>
          </tbody>
        </table>
      </div>
      <p class="count dim">共 {{ total }} 条{{ total > items.length ? `（显示前 ${items.length} 条）` : "" }}</p>
    </section>

    <!-- 右：详情 -->
    <aside class="detail-col">
      <div v-if="!selected" class="placeholder dim">
        <p>从左侧选择一条工单</p>
        <p class="tip">AI 无法确定或涉及高风险（退款 / 投诉 / 依赖故障）时，会自动转人工并在这里生成工单。</p>
      </div>

      <template v-else>
        <header class="head">
          <div>
            <h2 class="mono">{{ selected.ticket_id }}</h2>
            <p class="meta dim">
              {{ new Date(selected.created_at).toLocaleString() }} ·
              语言 {{ selected.language }} · 意图 {{ selected.intent }}
            </p>
          </div>
          <div class="chips">
            <span :class="['badge', PRIORITY_CLASS[selected.priority]]">{{ PRIORITY_LABEL[selected.priority] }}</span>
            <span :class="['badge', STATUS_CLASS[selected.status]]">{{ STATUS_LABEL[selected.status] }}</span>
          </div>
        </header>

        <div class="qa">
          <p class="label">买家问题</p>
          <p class="bubble-buyer">{{ selected.question }}</p>
          <p class="label">AI 已回复（或拟回复的话术）</p>
          <p class="bubble-agent">{{ selected.asked_answer }}</p>
        </div>

        <dl class="facts">
          <div><dt>转人工原因</dt><dd>{{ reasonLabel(selected.reason) }}</dd></div>
          <div><dt>置信度</dt><dd>{{ (selected.confidence * 100).toFixed(0) }}%</dd></div>
          <div><dt>首次响应</dt><dd>{{ fmt(selected.first_response_at) }}</dd></div>
          <div><dt>解决时间</dt><dd>{{ fmt(selected.resolved_at) }}</dd></div>
          <div><dt>trace_id</dt><dd class="mono">{{ selected.trace_id }}</dd></div>
          <div v-if="selected.session_id"><dt>会话</dt><dd class="mono">{{ selected.session_id }}</dd></div>
        </dl>

        <div v-if="selected.sources.length" class="sources">
          <p class="label">AI 检索到的来源（判断「该补知识库还是改检索」的依据）</p>
          <ul>
            <li v-for="s in selected.sources" :key="s" class="mono">{{ s }}</li>
          </ul>
        </div>

        <!-- 操作区（管理员） -->
        <div v-if="isAdmin" class="ops">
          <p class="label">处理</p>
          <div class="ops-row">
            <select
              :value="selected.status"
              :disabled="saving"
              @change="patch({ status: ($event.target as HTMLSelectElement).value as TicketStatus })"
            >
              <option value="open">待处理</option>
              <option value="in_progress">处理中</option>
              <option value="resolved">已解决</option>
              <option value="closed">已关闭</option>
            </select>
            <select
              :value="selected.priority"
              :disabled="saving"
              @change="patch({ priority: ($event.target as HTMLSelectElement).value as TicketPriority })"
            >
              <option value="urgent">紧急</option>
              <option value="high">高</option>
              <option value="normal">普通</option>
              <option value="low">低</option>
            </select>
            <button class="ghost" :disabled="saving" @click="claimSelf">指派给我</button>
            <span class="dim cur">当前处理人：{{ selected.assignee || "未指派" }}</span>
          </div>

          <p class="label">回复买家 / 内部备注</p>
          <textarea v-model="replyBody" rows="3" placeholder="输入回复内容…" />
          <div class="ops-row">
            <label class="chk"><input v-model="replyInternal" type="checkbox" /> 仅内部备注（买家不可见，不计入首次响应 SLA）</label>
            <button class="primary" :disabled="saving || !replyBody.trim()" @click="sendReply">
              {{ saving ? "提交中…" : "提交" }}
            </button>
          </div>
          <p v-if="actionErr" class="err">{{ actionErr }}</p>
        </div>
        <p v-else class="tip dim">当前角色为客服（只读）。状态流转 / 指派 / 回复需要管理员权限。</p>

        <div v-if="selected.replies.length" class="replies">
          <p class="label">处理记录（{{ selected.replies.length }}）</p>
          <div v-for="r in selected.replies" :key="r.id" :class="['reply', r.internal ? 'internal' : '']">
            <p class="meta dim">
              {{ r.author }} · {{ new Date(r.created_at).toLocaleString() }}
              <span v-if="r.internal" class="badge dim">内部备注</span>
            </p>
            <p class="body">{{ r.body }}</p>
          </div>
        </div>
      </template>
    </aside>
  </div>
</template>

<style scoped>
.tickets { flex: 1; min-width: 0; min-height: 0; display: flex; gap: 16px; }
.list-col { flex: 1.35; min-width: 0; display: flex; flex-direction: column; gap: 12px; }
.detail-col {
  flex: 1; min-width: 0; background: var(--c-panel); border: 1px solid var(--c-border);
  border-radius: var(--radius); padding: 16px; overflow-y: auto;
}

.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.card {
  background: var(--c-panel); border: 1px solid var(--c-border); border-radius: 10px;
  padding: 8px 12px; display: flex; flex-direction: column; gap: 2px;
}
.card .k { font-size: 11px; color: var(--c-text-dim); }
.card b { font-size: 18px; }
.card b.warn { color: var(--c-warn); }
.card b.err { color: var(--c-err); }

.filters { display: flex; flex-wrap: wrap; gap: 8px; }
.filters select, .filters input {
  border: 1px solid var(--c-border); border-radius: 8px; padding: 6px 9px;
  background: var(--c-panel); font-size: 12px; outline: none;
}
.filters input { flex: 1; min-width: 120px; }
.filters select:focus, .filters input:focus { border-color: var(--c-primary); }
.filters .ghost {
  background: transparent; border: 1px solid var(--c-border); padding: 6px 12px; font-size: 12px;
}
.filters .ghost:hover { border-color: var(--c-primary); color: var(--c-primary); }

.table-wrap {
  flex: 1; min-height: 0; overflow: auto;
  background: var(--c-panel); border: 1px solid var(--c-border); border-radius: var(--radius);
}
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--c-border); }
th {
  color: var(--c-text-dim); font-weight: 600; background: #f8f9fb;
  position: sticky; top: 0; z-index: 1;
}
tbody tr { cursor: pointer; }
tbody tr:hover { background: var(--c-primary-soft); }
tbody tr.on { background: var(--c-primary-soft); box-shadow: inset 2px 0 0 var(--c-primary); }
td.q { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mono { font-family: Menlo, Consolas, monospace; font-size: 11px; }
.dim { color: var(--c-text-dim); }
.empty { text-align: center; padding: 24px; }
.count { margin: 0; font-size: 11px; }
.err { font-size: 12px; color: var(--c-err); margin: 0; }

.placeholder { height: 100%; display: grid; place-content: center; text-align: center; gap: 8px; }
.placeholder .tip { font-size: 12px; max-width: 320px; line-height: 1.8; }

.head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.head h2 { margin: 0 0 4px; font-size: 15px; }
.head .meta { margin: 0; font-size: 11px; }
.chips { display: flex; gap: 5px; flex: none; }

.label { font-size: 12px; color: var(--c-text-dim); font-weight: 600; margin: 14px 0 6px; }
.qa .bubble-buyer, .qa .bubble-agent {
  margin: 0; padding: 8px 11px; border-radius: 10px; font-size: 13px; line-height: 1.7;
  white-space: pre-wrap; word-break: break-word;
}
.bubble-buyer { background: var(--c-primary-soft); }
.bubble-agent { background: #f8f9fb; border: 1px solid var(--c-border); }

.facts { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 14px; margin: 14px 0 0; }
.facts > div { display: flex; gap: 8px; font-size: 12px; }
.facts dt { color: var(--c-text-dim); flex: none; }
.facts dd { margin: 0; word-break: break-all; }

.sources ul { margin: 0; padding-left: 18px; font-size: 12px; color: var(--c-text-dim); }

.ops { border-top: 1px dashed var(--c-border); margin-top: 14px; }
.ops-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px; }
.ops-row select {
  border: 1px solid var(--c-border); border-radius: 8px; padding: 6px 9px;
  background: var(--c-panel); font-size: 12px;
}
.ops .ghost { background: transparent; border: 1px solid var(--c-border); padding: 6px 12px; font-size: 12px; }
.ops .ghost:hover { border-color: var(--c-primary); color: var(--c-primary); }
.ops .primary {
  background: var(--c-primary); color: #fff; padding: 6px 14px; font-size: 12px; margin-left: auto;
}
.ops .primary:disabled { opacity: 0.5; cursor: not-allowed; }
.ops textarea {
  width: 100%; border: 1px solid var(--c-border); border-radius: 8px;
  padding: 8px 10px; font-size: 13px; outline: none; resize: vertical;
}
.ops textarea:focus { border-color: var(--c-primary); }
.chk { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--c-text-dim); }
.cur { font-size: 11px; }

.replies { margin-top: 16px; border-top: 1px dashed var(--c-border); }
.reply { padding: 8px 10px; border-radius: 8px; background: #f8f9fb; margin-bottom: 8px; }
.reply.internal { background: #fdf1e0; }
.reply .meta { margin: 0 0 4px; font-size: 11px; }
.reply .body { margin: 0; font-size: 13px; white-space: pre-wrap; word-break: break-word; }
.tip { font-size: 12px; line-height: 1.8; }

@media (max-width: 1100px) {
  .tickets { flex-direction: column; }
  .cards { grid-template-columns: repeat(2, 1fr); }
}
</style>
