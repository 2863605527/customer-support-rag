<script setup lang="ts">
/**
 * 客服工作台：左侧对话区 + 右侧信息栏（会话、历史、快捷提问、服务状态）。
 *
 * 顶栏、登录态、改密/审计弹窗都已上移到 ``AppLayout``，本页只负责工作台内容。
 */
import ChatPanel from "@/components/ChatPanel.vue";
import SidePanel from "@/components/SidePanel.vue";
</script>

<template>
  <div class="workbench">
    <ChatPanel class="chat" />
    <SidePanel class="side" />
  </div>
</template>

<style scoped>
.workbench { flex: 1; min-width: 0; min-height: 0; display: flex; gap: 16px; }
.chat { flex: 1; min-width: 0; }
.side { width: 300px; flex-shrink: 0; }

@media (max-width: 900px) {
  .workbench { flex-direction: column; }
  .side { width: auto; }
}
</style>
