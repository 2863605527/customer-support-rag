<script setup lang="ts">
/**
 * 应用外壳：顶栏（品牌 / 导航 / 服务状态 / 用户菜单）+ 路由出口。
 *
 * 把导航抽到这一层，是为了让「工作台 / 工单 / 运营仪表盘」共用同一套顶栏与登录态，
 * 页面组件只关心自己的内容区。改密码 / 登录审计这类全局动作也集中在这里，
 * 避免每个页面各写一份弹窗。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { authApi, humanizeError } from "@/api/client";
import { useAuth } from "@/composables/useAuth";
import { useHealth } from "@/composables/useHealth";
import type { AuditEntry } from "@/types";

const { currentUser, logout } = useAuth();
const router = useRouter();
const route = useRoute();
const { ready, reachable, statusText, docCount, llmQuotaExhausted, start, stop } = useHealth();

const menuOpen = ref(false);
const dialog = ref<"none" | "password" | "audit">("none");

const isAdmin = computed(() => currentUser.value?.role === "admin");
const displayName = computed(() => currentUser.value?.full_name || currentUser.value?.email || "未登录");

const navItems = computed(() => {
  const all = [
    { name: "workspace", label: "工作台", adminOnly: false },
    { name: "tickets", label: "工单中心", adminOnly: false },
    { name: "dashboard", label: "运营仪表盘", adminOnly: true },
  ];
  return all.filter((item) => !item.adminOnly || isAdmin.value);
});

function isActive(name: string): boolean {
  return route.name === name;
}

// ---- 改密 ----
const oldPassword = ref("");
const newPassword = ref("");
const confirmPassword = ref("");
const passwordMsg = ref("");
const passwordErr = ref("");
const passwordBusy = ref(false);

async function submitPassword(): Promise<void> {
  passwordMsg.value = "";
  passwordErr.value = "";
  if (newPassword.value !== confirmPassword.value) {
    passwordErr.value = "两次输入的新密码不一致";
    return;
  }
  passwordBusy.value = true;
  try {
    await authApi.changePassword(oldPassword.value, newPassword.value);
    passwordMsg.value = "密码已更新，其他设备的登录已失效，本机令牌将在下次过期时自动续期。";
    oldPassword.value = newPassword.value = confirmPassword.value = "";
  } catch (err) {
    passwordErr.value = humanizeError(err);
  } finally {
    passwordBusy.value = false;
  }
}

// ---- 审计（管理员） ----
const audit = ref<AuditEntry[]>([]);
const auditErr = ref("");

async function openAudit(): Promise<void> {
  dialog.value = "audit";
  auditErr.value = "";
  try {
    audit.value = await authApi.auditLogs(50);
  } catch (err) {
    auditErr.value = humanizeError(err);
  }
}

const EVENT_LABEL: Record<string, string> = {
  register: "注册",
  login_success: "登录成功",
  login_failed: "登录失败",
  login_locked: "账号锁定",
  logout: "登出",
  refresh: "令牌刷新",
  password_changed: "修改密码",
};

function eventLabel(event: string): string {
  return EVENT_LABEL[event] ?? event;
}

async function onLogout(): Promise<void> {
  await logout();
  await router.replace({ name: "login" });
}

onMounted(() => start());
onBeforeUnmount(() => {
  stop();
  menuOpen.value = false;
});
</script>

<template>
  <div class="layout">
    <header class="topbar">
      <div class="brand">
        <span class="logo">くらし寝具</span>
        <span class="sub">智能客服工作台</span>
      </div>

      <nav class="nav">
        <RouterLink
          v-for="item in navItems"
          :key="item.name"
          class="nav-item"
          :class="{ on: isActive(item.name) }"
          :to="{ name: item.name }"
        >
          {{ item.label }}
        </RouterLink>
      </nav>

      <div class="right">
        <div class="status">
          <span class="dot" :class="reachable === false ? 'err' : ready?.ready ? 'ok' : 'warn'" />
          <span>{{ statusText }}</span>
          <span v-if="docCount !== null" class="dim">知识库 {{ docCount }} 篇</span>
          <span v-if="llmQuotaExhausted" class="badge err" title="LLM 账户余额/配额不足，请充值或更换 key">
            LLM 欠费
          </span>
        </div>

        <div class="user">
          <button class="chip" @click="menuOpen = !menuOpen">
            <span class="avatar">{{ displayName.slice(0, 1).toUpperCase() }}</span>
            <span class="who">
              <b>{{ displayName }}</b>
              <i>{{ currentUser?.role === "admin" ? "管理员" : "客服" }}</i>
            </span>
            <span class="caret">▾</span>
          </button>

          <div v-if="menuOpen" class="menu" @click="menuOpen = false">
            <p class="mail">{{ currentUser?.email }}</p>
            <button @click="dialog = 'password'">修改密码</button>
            <button v-if="isAdmin" @click="openAudit">登录审计（管理员）</button>
            <button class="danger" @click="onLogout">退出登录</button>
          </div>
        </div>
      </div>
    </header>

    <main class="content">
      <RouterView />
    </main>

    <!-- 修改密码 -->
    <div v-if="dialog === 'password'" class="mask" @click.self="dialog = 'none'">
      <div class="modal">
        <h2>修改密码</h2>
        <p class="tip">修改后该账号在所有设备上的刷新令牌都会失效，需要重新登录。</p>
        <label class="field"><span>当前密码</span>
          <input v-model="oldPassword" type="password" autocomplete="current-password" />
        </label>
        <label class="field"><span>新密码</span>
          <input v-model="newPassword" type="password" autocomplete="new-password" />
        </label>
        <label class="field"><span>确认新密码</span>
          <input v-model="confirmPassword" type="password" autocomplete="new-password" />
        </label>
        <p v-if="passwordErr" class="msg err">{{ passwordErr }}</p>
        <p v-if="passwordMsg" class="msg ok">{{ passwordMsg }}</p>
        <div class="actions">
          <button class="ghost" @click="dialog = 'none'">关闭</button>
          <button class="primary" :disabled="passwordBusy" @click="submitPassword">
            {{ passwordBusy ? "提交中…" : "确认修改" }}
          </button>
        </div>
      </div>
    </div>

    <!-- 登录审计（管理员） -->
    <div v-if="dialog === 'audit'" class="mask" @click.self="dialog = 'none'">
      <div class="modal wide">
        <h2>登录审计日志</h2>
        <p class="tip">最近的注册 / 登录成功失败 / 锁定 / 登出 / 令牌刷新记录，含来源 IP。</p>
        <p v-if="auditErr" class="msg err">{{ auditErr }}</p>
        <table v-else>
          <thead>
            <tr><th>时间</th><th>邮箱</th><th>事件</th><th>IP</th><th>详情</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in audit" :key="row.id">
              <td class="mono">{{ new Date(row.created_at).toLocaleString() }}</td>
              <td>{{ row.email }}</td>
              <td>
                <span class="badge" :class="row.event === 'login_success' ? 'ok' : row.event === 'login_failed' ? 'err' : 'dim'">
                  {{ eventLabel(row.event) }}
                </span>
              </td>
              <td class="mono">{{ row.ip || "-" }}</td>
              <td class="dim">{{ row.detail }}</td>
            </tr>
            <tr v-if="!audit.length"><td colspan="5" class="dim">暂无记录</td></tr>
          </tbody>
        </table>
        <div class="actions">
          <button class="ghost" @click="dialog = 'none'">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.layout { height: 100%; display: flex; flex-direction: column; }
.topbar {
  display: flex; align-items: center; gap: 26px;
  padding: 0 20px; height: 54px; flex: none;
  background: var(--c-panel); border-bottom: 1px solid var(--c-border);
}
.brand { display: flex; align-items: baseline; gap: 9px; }
.logo { font-weight: 700; font-size: 16px; color: var(--c-primary); }
.sub { color: var(--c-text-dim); font-size: 12px; }

.nav { display: flex; align-items: center; gap: 4px; }
.nav-item {
  padding: 7px 14px; border-radius: 8px; font-size: 13px;
  color: var(--c-text-dim); text-decoration: none; white-space: nowrap;
}
.nav-item:hover { background: var(--c-primary-soft); color: var(--c-primary); }
.nav-item.on { background: var(--c-primary-soft); color: var(--c-primary); font-weight: 600; }

.right { margin-left: auto; display: flex; align-items: center; gap: 16px; }
.status { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.dot.ok { background: var(--c-ok); }
.dot.warn { background: var(--c-warn); }
.dot.err { background: var(--c-err); }
.dim { color: var(--c-text-dim); }

.user { position: relative; }
.chip {
  display: flex; align-items: center; gap: 8px;
  background: transparent; border: 1px solid var(--c-border); padding: 5px 10px;
}
.chip:hover { border-color: var(--c-primary); }
.avatar {
  width: 24px; height: 24px; border-radius: 50%;
  background: var(--c-primary-soft); color: var(--c-primary);
  display: grid; place-items: center; font-size: 12px; font-weight: 700;
}
.who { display: flex; flex-direction: column; line-height: 1.15; text-align: left; }
.who b { font-size: 12px; font-weight: 600; }
.who i { font-size: 10px; font-style: normal; color: var(--c-text-dim); }
.caret { color: var(--c-text-dim); font-size: 10px; }

.menu {
  position: absolute; right: 0; top: calc(100% + 6px); z-index: 20;
  background: var(--c-panel); border: 1px solid var(--c-border); border-radius: 10px;
  box-shadow: 0 10px 26px rgba(27, 39, 71, 0.12); padding: 6px; min-width: 190px;
}
.menu .mail { margin: 4px 8px 8px; font-size: 11px; color: var(--c-text-dim); word-break: break-all; }
.menu button {
  display: block; width: 100%; text-align: left; padding: 8px 10px;
  background: transparent; font-size: 13px;
}
.menu button:hover { background: var(--c-primary-soft); color: var(--c-primary); }
.menu button.danger:hover { background: #fbe9e7; color: var(--c-err); }

.content { flex: 1; min-height: 0; padding: 16px; display: flex; }

.mask {
  position: fixed; inset: 0; background: rgba(20, 28, 44, 0.42);
  display: grid; place-items: center; padding: 20px; z-index: 40;
}
.modal {
  width: 100%; max-width: 420px; background: var(--c-panel);
  border-radius: 14px; padding: 22px; max-height: 88vh; overflow: auto;
}
.modal.wide { max-width: 820px; }
.modal h2 { margin: 0 0 6px; font-size: 17px; }
.modal .tip { margin: 0 0 14px; font-size: 12px; color: var(--c-text-dim); line-height: 1.7; }
.field { display: block; margin-bottom: 12px; }
.field > span { display: block; font-size: 12px; color: var(--c-text-dim); margin-bottom: 5px; }
.field input {
  width: 100%; padding: 9px 11px; border: 1px solid var(--c-border);
  border-radius: 8px; outline: none;
}
.field input:focus { border-color: var(--c-primary); }
.msg { font-size: 12px; margin: 4px 0 8px; line-height: 1.6; }
.msg.err { color: var(--c-err); }
.msg.ok { color: var(--c-ok); }
.actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }
.actions .ghost { background: transparent; border: 1px solid var(--c-border); padding: 8px 16px; }
.actions .primary { background: var(--c-primary); color: #fff; padding: 8px 18px; }
.actions .primary:disabled { opacity: 0.5; cursor: not-allowed; }

table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--c-border); }
th { color: var(--c-text-dim); font-weight: 600; background: #f8f9fb; }
.mono { font-family: Menlo, Consolas, monospace; font-size: 11px; }

@media (max-width: 900px) {
  .sub { display: none; }
  .status .dim { display: none; }
}
</style>
