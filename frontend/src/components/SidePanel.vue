<script setup lang="ts">
/**
 * 工作台右侧信息栏：当前会话、历史会话复盘、快捷提问、服务状态。
 *
 * 历史会话读的是**服务端**记录（``/ops/conversations``），所以在任何一台设备登录
 * 都能回看同一批对话——本地 localStorage 那份只是当前浏览器的渲染快照。
 */
import { computed, onMounted, ref } from "vue";
import { consoleApi, humanizeError } from "@/api/client";
import { useChat } from "@/composables/useChat";
import { useHealth } from "@/composables/useHealth";
import type { ConversationSummary } from "@/types";

const { ready, reachable, retrievalOk, llmOk, llmQuotaExhausted } = useHealth();
const { sessionId, send, clearSession, openConversation } = useChat();

// 构建版本标识（vite.config.ts 注入），展示在侧栏底部
const buildInfo = __BUILD_INFO__;

// 演示用快捷问题：覆盖「正常作答 / 多语言 / 资料不足 / 高风险」四类路径
const samples = [
  "返品はできますか？",
  "送料はいくらですか？",
  "Do you ship to Osaka?",
  "アルミ合金の蚊帳はありますか？",
  "今日の天気は？",
  "商品が壊れていました。最悪です、返金しろ！",
];

// ---- 历史会话（服务端） ----
const history = ref<ConversationSummary[]>([]);
const historyErr = ref("");
const historyBusy = ref(false);
const historyOpen = ref(false);

async function loadHistory(): Promise<void> {
  historyBusy.value = true;
  historyErr.value = "";
  try {
    const page = await consoleApi.conversations({ limit: 20 });
    history.value = page.items;
  } catch (err) {
    historyErr.value = humanizeError(err);
  } finally {
    historyBusy.value = false;
  }
}

async function toggleHistory(): Promise<void> {
  historyOpen.value = !historyOpen.value;
  if (historyOpen.value) await loadHistory();
}

async function restore(target: string): Promise<void> {
  try {
    await openConversation(target);
  } catch (err) {
    historyErr.value = humanizeError(err);
  }
}

function relTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return `${Math.floor(diff / 86_400_000)} 天前`;
}

const currentInHistory = computed(() => history.value.some((c) => c.session_id === sessionId.value));

onMounted(() => void loadHistory());
</script>

<template>
  <aside class="panel">
    <section class="block">
      <h3>会话</h3>
      <p class="sid" :title="sessionId">{{ sessionId }}</p>
      <button class="ghost" @click="clearSession">新会话（清空记忆）</button>
    </section>

    <section class="block">
      <h3>
        历史会话
        <button class="mini" :disabled="historyBusy" @click="loadHistory">
          {{ historyBusy ? "…" : "刷新" }}
        </button>
      </h3>
      <p v-if="historyErr" class="err">{{ historyErr }}</p>
      <p v-else-if="!history.length" class="hint">暂无服务端历史（提一个问题后就有了）</p>
      <ul v-else :class="['history', historyOpen ? 'open' : '']">
        <li
          v-for="c in history"
          :key="c.session_id"
          :class="['row', c.session_id === sessionId ? 'on' : '']"
          :title="c.session_id"
          @click="restore(c.session_id)"
        >
          <span class="q">{{ c.last_question || "（无问题）" }}</span>
          <span class="meta">
            {{ relTime(c.updated_at) }} · {{ c.message_count }} 条
            <span v-if="c.handoff_count" class="badge warn">转人工 {{ c.handoff_count }}</span>
          </span>
        </li>
      </ul>
      <button v-if="history.length > 5" class="link" @click="toggleHistory">
        {{ historyOpen ? "收起" : `展开全部 ${history.length} 条` }}
      </button>
      <p v-if="currentInHistory" class="hint">当前会话已在列表中（高亮行）</p>
    </section>

    <section class="block">
      <h3>快捷提问</h3>
      <div class="samples">
        <button v-for="q in samples" :key="q" class="sample" @click="send(q)">
          {{ q }}
        </button>
      </div>
    </section>

    <section class="block">
      <h3>服务状态</h3>
      <ul class="health">
        <li>
          后端连接
          <span :class="['badge', reachable ? 'ok' : 'err']">
            {{ reachable === null ? "检测中" : reachable ? "正常" : "失败" }}
          </span>
        </li>
        <li>
          LLM 凭据
          <span :class="['badge', ready?.checks.llm_configured ? 'ok' : 'err']">
            {{ ready?.checks.llm_configured ? "已配置" : "未配置" }}
          </span>
        </li>
        <li>
          LLM 可用性
          <span :class="['badge', llmOk ? 'ok' : 'err']">
            {{ llmQuotaExhausted ? "欠费/超额" : llmOk ? "正常" : "异常" }}
          </span>
        </li>
        <li>
          检索后端
          <span :class="['badge', retrievalOk ? 'ok' : 'err']">
            {{ retrievalOk ? "正常" : "不可用" }}
          </span>
        </li>
        <li class="dim" v-if="ready?.checks.retrieval?.count !== undefined">
          知识库文档：{{ ready.checks.retrieval.count }} 篇
        </li>
        <li class="dim" v-if="ready?.checks.retrieval?.error">
          {{ ready.checks.retrieval.error }}
        </li>
        <li class="dim" v-if="llmQuotaExhausted && ready?.checks.llm?.last_error">
          {{ ready.checks.llm.last_error }}
        </li>
        <li class="dim" v-if="reachable === false">
          连接失败：请确认打开的是正确端口——后端直营模式 http://127.0.0.1:8002/（重启后端生效），
          开发模式 npm run dev 后访问 Vite 打印的地址（注意 5173/5174 可能被其他项目占用）。
        </li>
      </ul>
    </section>

    <section class="block mt-auto">
      <p class="ver" title="如果这个值和最新构建不一致，说明浏览器在用缓存的旧页面，请强制刷新">{{ buildInfo }}</p>
    </section>
  </aside>
</template>

<style scoped>
.panel {
  height: 100%;
  overflow-y: auto;
  background: var(--c-panel);
  border: 1px solid var(--c-border);
  border-radius: var(--radius);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.block h3 {
  display: flex; align-items: center; justify-content: space-between;
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--c-text-dim);
  font-weight: 600;
}
.sid {
  font-size: 12px;
  color: var(--c-text-dim);
  word-break: break-all;
  margin: 0 0 8px;
}
.ghost {
  width: 100%;
  padding: 7px 0;
  background: transparent;
  border: 1px solid var(--c-border);
  color: var(--c-text);
}
.ghost:hover { border-color: var(--c-primary); color: var(--c-primary); }

.mini {
  background: transparent; border: 1px solid var(--c-border);
  color: var(--c-text-dim); font-size: 11px; padding: 2px 8px; font-weight: 400;
}
.mini:hover { border-color: var(--c-primary); color: var(--c-primary); }

.history { list-style: none; margin: 0; padding: 0; max-height: 176px; overflow-y: auto; }
.history.open { max-height: none; }
.history .row {
  display: flex; flex-direction: column; gap: 2px;
  padding: 7px 8px; border-radius: 8px; cursor: pointer;
}
.history .row:hover { background: var(--c-primary-soft); }
.history .row.on { background: var(--c-primary-soft); box-shadow: inset 2px 0 0 var(--c-primary); }
.history .q {
  font-size: 12px; color: var(--c-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.history .meta { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--c-text-dim); }
.link {
  background: transparent; border: none; color: var(--c-primary);
  font-size: 12px; padding: 6px 0 0; text-decoration: underline;
}
.hint { font-size: 12px; color: var(--c-text-dim); margin: 0; }
.err { font-size: 12px; color: var(--c-err); margin: 0; }

.samples { display: flex; flex-wrap: wrap; gap: 6px; }
.sample {
  padding: 5px 10px;
  background: var(--c-primary-soft);
  color: var(--c-primary);
  font-size: 12px;
  text-align: left;
}
.sample:hover { filter: brightness(0.96); }
.health { list-style: none; margin: 0; padding: 0; }
.health li {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 5px 0;
  font-size: 13px;
}
.health li.dim { color: var(--c-text-dim); font-size: 12px; justify-content: flex-start; word-break: break-all; }
.mt-auto { margin-top: auto; }
.ver {
  margin: 0; font-size: 11px; color: var(--c-text-dim);
  text-align: center; word-break: break-all;
}
</style>
