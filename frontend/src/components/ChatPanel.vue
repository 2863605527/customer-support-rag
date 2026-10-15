<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useChat } from "@/composables/useChat";
import MessageItem from "@/components/MessageItem.vue";
import Composer from "@/components/Composer.vue";

const { messages, sending, send } = useChat();
const listEl = ref<HTMLElement | null>(null);

const lastMsg = computed(() => messages.value[messages.value.length - 1]);

// 新消息 + 流式增量（文本变长/思考步骤推进）都跟随滚动到底部
watch(
  [
    () => messages.value.length,
    () => lastMsg.value?.text.length,
    () => lastMsg.value?.steps?.length,
  ],
  async () => {
    await nextTick();
    const el = listEl.value;
    if (el) el.scrollTop = el.scrollHeight;
  },
);
</script>

<template>
  <section class="panel">
    <div ref="listEl" class="list">
      <div v-if="messages.length === 0" class="empty">
        <p class="title">模拟一位买家，向客服提问</p>
        <p class="hint">
          支持日语 / 英语 / 中文等多语言。检索命中知识库时直接作答并附引用；
          资料不足、高风险诉求（退货退款纠纷等）会自动转人工并生成工单。
        </p>
      </div>
      <MessageItem v-for="(m, i) in messages" :key="i" :msg="m" />
    </div>
    <Composer :sending="sending" @send="send" />
  </section>
</template>

<style scoped>
.panel {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--c-panel);
  border: 1px solid var(--c-border);
  border-radius: var(--radius);
  overflow: hidden;
}
.list {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.empty {
  margin: auto;
  text-align: center;
  color: var(--c-text-dim);
  max-width: 420px;
}
.empty .title { font-size: 15px; color: var(--c-text); margin-bottom: 6px; }
.empty .hint { font-size: 12px; line-height: 1.7; }
</style>
