<script setup lang="ts">
import { ref } from "vue";
import { feedbackApi } from "@/api/client";
import type { ChatUiMessage } from "@/types";

const props = defineProps<{ msg: ChatUiMessage }>();

// 引用列表默认收起，避免干扰阅读
const showRefs = ref(false);

// ---- 质量反馈：👍/👎 + 原因标签，直接喂给后端做质量闭环 ----
const rated = ref<"up" | "down" | null>(null);
const pickerOpen = ref(false);
const reasonLabels = ref<Record<string, string>>({});
const picked = ref<string[]>([]);
const comment = ref("");
const busy = ref(false);
const fbError = ref("");

function toggleReason(code: string): void {
  picked.value = picked.value.includes(code)
    ? picked.value.filter((c) => c !== code)
    : [...picked.value, code];
}

async function loadReasons(rating: "up" | "down"): Promise<void> {
  if (Object.keys(reasonLabels.value).length) return;
  try {
    const all = await feedbackApi.reasons();
    reasonLabels.value = rating === "down" ? all.down : all.up;
  } catch {
    reasonLabels.value = {};
  }
}

async function rateUp(): Promise<void> {
  await submit("up", []);
}

function openDownPicker(): void {
  pickerOpen.value = true;
  picked.value = [];
  void loadReasons("down");
}

async function submit(rating: "up" | "down", reasons: string[]): Promise<void> {
  const r = props.msg.result;
  if (!r || !r.trace_id || busy.value) return;
  busy.value = true;
  fbError.value = "";
  try {
    await feedbackApi.submit({
      trace_id: r.trace_id,
      rating,
      reasons,
      comment: comment.value,
      session_id: r.session_id,
      question: r.question,
      answer: r.answer,
      source: r.source,
    });
    rated.value = rating;
    pickerOpen.value = false;
    picked.value = [];
    comment.value = "";
  } catch (err) {
    fbError.value = err instanceof Error ? err.message : "反馈提交失败";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="msg" :class="msg.role">
    <div class="bubble">
      <!-- 还没有任何阶段信息时才显示纯打字点 -->
      <span v-if="msg.pending && !msg.text && !(msg.steps && msg.steps.length)" class="typing">
        <i /><i /><i />
      </span>
      <template v-else>
        <!-- 思考过程：进行中逐步点亮，完成后折叠可展开（DeepSeek 式） -->
        <details v-if="msg.steps && msg.steps.length" class="think" :open="msg.pending">
          <summary>
            <template v-if="msg.pending">思考过程（{{ msg.steps.length }} 步）</template>
            <template v-else>
              已完成思考（{{ msg.steps.length }} 步<template v-if="msg.result">
                · 用时 {{ (msg.result.latency_ms / 1000).toFixed(1) }}s</template
              >）
            </template>
          </summary>
          <ul class="steps">
            <li v-for="(s, i) in msg.steps" :key="i" :class="s.state">
              <span class="icon">
                <template v-if="s.state === 'done'">✓</template>
                <template v-else><span class="pulse" /></template>
              </span>
              <span class="label">{{ s.label }}</span>
              <span v-if="s.detail" class="detail">{{ s.detail }}</span>
            </li>
          </ul>
        </details>

        <p class="text" :class="{ err: msg.error }">{{ msg.text }}<span v-if="msg.streaming" class="cursor" /></p>

        <details v-if="msg.result" class="meta" ref="meta">
          <summary>
            <span class="badge dim">{{ msg.result.source || "unknown" }}</span>
            <span v-if="msg.result.cached" class="badge ok">缓存命中</span>
            <span class="badge">{{ msg.result.intent }} {{ (msg.result.confidence * 100).toFixed(0) }}%</span>
            <span :class="['badge', msg.result.need_human ? 'err' : 'ok']">
              {{ msg.result.need_human ? "已转人工" : "AI 应答" }}
            </span>
            <span v-if="msg.result.ticket_id" class="badge warn">工单 {{ msg.result.ticket_id }}</span>
            <span class="badge dim">{{ msg.result.language }} · {{ msg.result.latency_ms }}ms</span>
            <span v-if="msg.result.prompt_tokens" class="badge dim">
              {{ msg.result.prompt_tokens }}+{{ msg.result.completion_tokens }} tokens
            </span>
          </summary>

          <div class="detail">
            <p v-if="msg.result.human_reason" class="line">转人工原因：{{ msg.result.human_reason }}</p>
            <p class="line">trace_id：{{ msg.result.trace_id }}（报障时提供给开发对日志）</p>
            <p v-if="msg.result.stage_ms && Object.keys(msg.result.stage_ms).length" class="line">
              分阶段耗时：{{ Object.entries(msg.result.stage_ms).map(([k, v]) => `${k} ${v}ms`).join(" · ") }}
            </p>

            <template v-if="msg.result.references.length">
              <p class="line refs-toggle">
                <a href="#" @click.prevent="showRefs = !showRefs">
                  引用来源（{{ msg.result.references.length }}条）{{ showRefs ? "收起 ▲" : "展开 ▼" }}
                </a>
              </p>
              <ul v-show="showRefs" class="refs">
                <li v-for="(r, i) in msg.result.references" :key="i">
                  <span class="src">{{ r.source }}</span>
                  <span class="score">{{ r.score.toFixed(3) }}</span>
                  <span class="snippet">{{ r.snippet }}</span>
                </li>
              </ul>
            </template>
          </div>
        </details>

        <!-- 质量反馈：好评率是 RAG 调优的抓手，所以放在每条 AI 回答下方（不藏在折叠里） -->
        <div v-if="msg.result && !msg.result.need_human" class="feedback">
          <template v-if="rated">
            <span class="thanks">已记录你的评价（{{ rated === "up" ? "有帮助" : "没帮助" }}）</span>
            <button class="link" @click="rated = null">改一下</button>
          </template>
          <template v-else>
            <span class="ask">这条回答有帮助吗？</span>
            <button class="fb" :disabled="busy" title="有帮助" @click="rateUp">👍</button>
            <button class="fb" :disabled="busy" title="没帮助" @click="openDownPicker">👎</button>
          </template>

          <div v-if="pickerOpen && !rated" class="picker">
            <span class="ask">哪里需要改进？（可多选）</span>
            <div class="tags">
              <button
                v-for="(label, code) in reasonLabels"
                :key="code"
                :class="['tag', picked.includes(String(code)) ? 'on' : '']"
                @click="toggleReason(String(code))"
              >
                {{ label }}
              </button>
            </div>
            <input v-model="comment" class="comment" maxlength="200" placeholder="补充说明（可选）" />
            <div class="picker-actions">
              <button class="link" @click="pickerOpen = false">取消</button>
              <button
                class="send"
                :disabled="busy || picked.length === 0"
                @click="submit('down', picked)"
              >
                提交
              </button>
            </div>
          </div>
          <p v-if="fbError" class="fb-err">{{ fbError }}</p>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.msg { display: flex; }
.msg.buyer { justify-content: flex-end; }
.bubble {
  max-width: 78%;
  border-radius: var(--radius);
  padding: 10px 14px;
  line-height: 1.65;
}
.buyer .bubble { background: var(--c-buyer-bg); color: #fff; border-bottom-right-radius: 4px; }
.agent .bubble { background: var(--c-agent-bg); border: 1px solid var(--c-border); border-bottom-left-radius: 4px; }
.text { margin: 0; white-space: pre-wrap; word-break: break-word; }
.text.err { color: var(--c-err); }

.typing { display: inline-flex; gap: 5px; padding: 4px 0; }
.typing i {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--c-text-dim);
  animation: blink 1.2s infinite ease-in-out;
}
.typing i:nth-child(2) { animation-delay: 0.2s; }
.typing i:nth-child(3) { animation-delay: 0.4s; }
@keyframes blink { 0%, 80%, 100% { opacity: 0.25; } 40% { opacity: 1; } }

/* 思考过程：折叠面板 + 逐步点亮的步骤列表 */
.think { margin: 0 0 6px; font-size: 12px; color: var(--c-text-dim); }
.think summary {
  cursor: pointer; user-select: none;
  list-style: none;
  display: inline-flex; align-items: center; gap: 4px;
}
.think summary::-webkit-details-marker { display: none; }
.think summary::before { content: "▸"; font-size: 10px; transition: transform 0.15s; }
.think[open] summary::before { transform: rotate(90deg); }
.steps { list-style: none; margin: 6px 0 0; padding: 0 0 0 4px; }
.steps li { display: flex; align-items: baseline; gap: 6px; padding: 2px 0; }
.steps li .icon { width: 14px; text-align: center; flex: none; }
.steps li.done .icon { color: var(--c-ok, #16a34a); }
.steps li.active { color: var(--c-text); }
.pulse {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--c-primary);
  animation: pulse 1s infinite ease-in-out;
}
@keyframes pulse { 0%, 100% { transform: scale(0.8); opacity: 0.4; } 50% { transform: scale(1.15); opacity: 1; } }
.steps .detail { color: var(--c-text-dim); }
.steps .detail::before { content: "·"; margin-right: 4px; }

/* 打字光标：流式渲染中跟随文本末尾 */
.cursor {
  display: inline-block; width: 2px; height: 1em;
  margin-left: 2px; vertical-align: -0.15em;
  background: var(--c-primary);
  animation: caret 0.9s steps(2) infinite;
}
@keyframes caret { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0; } }

.meta { margin-top: 8px; font-size: 12px; }
.meta summary {
  display: flex; flex-wrap: wrap; gap: 5px;
  cursor: pointer; list-style: none;
}
.meta summary::-webkit-details-marker { display: none; }
.meta .detail { margin-top: 8px; border-top: 1px dashed var(--c-border); padding-top: 8px; }
.line { margin: 4px 0; color: var(--c-text-dim); }
.refs-toggle a { color: var(--c-primary); text-decoration: none; }
.refs { margin: 6px 0 0; padding-left: 16px; }
.refs li { margin: 6px 0; display: flex; gap: 8px; align-items: baseline; }
.src { color: var(--c-primary); font-weight: 600; white-space: nowrap; }
.score { color: var(--c-text-dim); white-space: nowrap; }
.snippet { color: var(--c-text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 质量反馈 */
.feedback {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  margin-top: 8px; padding-top: 8px; font-size: 12px; color: var(--c-text-dim);
  border-top: 1px dashed var(--c-border);
}
.feedback .ask { color: var(--c-text-dim); }
.fb {
  border: 1px solid var(--c-border); background: transparent;
  border-radius: 6px; padding: 1px 7px; font-size: 13px; line-height: 1.5;
}
.fb:hover { border-color: var(--c-primary); }
.fb:disabled { opacity: 0.5; cursor: not-allowed; }
.thanks { color: var(--c-ok); }
.link {
  background: transparent; border: none; color: var(--c-primary);
  font-size: 12px; padding: 0; text-decoration: underline;
}
.picker {
  flex-basis: 100%; display: flex; flex-direction: column; gap: 6px;
  background: #f8f9fb; border: 1px solid var(--c-border); border-radius: 8px; padding: 8px 10px;
}
.tags { display: flex; flex-wrap: wrap; gap: 5px; }
.tag {
  border: 1px solid var(--c-border); background: #fff; color: var(--c-text);
  border-radius: 999px; padding: 2px 9px; font-size: 12px;
}
.tag.on { border-color: var(--c-primary); color: var(--c-primary); background: var(--c-primary-soft); }
.comment {
  border: 1px solid var(--c-border); border-radius: 6px; padding: 4px 8px; font-size: 12px; outline: none;
}
.comment:focus { border-color: var(--c-primary); }
.picker-actions { display: flex; justify-content: flex-end; gap: 12px; align-items: center; }
.send {
  background: var(--c-primary); color: #fff; border: none;
  border-radius: 6px; padding: 3px 12px; font-size: 12px;
}
.send:disabled { opacity: 0.5; cursor: not-allowed; }
.fb-err { flex-basis: 100%; color: var(--c-err); margin: 0; }
</style>
