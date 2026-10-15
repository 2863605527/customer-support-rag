/** 路由与登录守卫。
 *
 * 守卫策略：**默认全部需要登录**，只有 ``meta.public`` 的页面（登录/注册）可匿名访问。
 * 白名单而不是黑名单——新增页面忘了加标记时，会「多要一次登录」而不是「漏保护」。
 *
 * 布局：登录/注册是独立整页；其余页面统一挂在 ``AppLayout`` 下（共用顶栏与导航）。
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import AppLayout from "@/layouts/AppLayout.vue";
import { getAccessToken, getCurrentUser, refreshAccessToken } from "@/composables/useAuth";

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: () => import("@/views/LoginView.vue"),
    meta: { public: true, title: "登录" },
  },
  {
    path: "/register",
    name: "register",
    component: () => import("@/views/RegisterView.vue"),
    meta: { public: true, title: "注册" },
  },
  {
    path: "/",
    component: AppLayout,
    children: [
      {
        path: "",
        name: "workspace",
        component: () => import("@/views/WorkspaceView.vue"),
        meta: { title: "客服工作台" },
      },
      {
        path: "tickets",
        name: "tickets",
        component: () => import("@/views/TicketsView.vue"),
        meta: { title: "工单中心" },
      },
      {
        path: "dashboard",
        name: "dashboard",
        component: () => import("@/views/DashboardView.vue"),
        meta: { title: "运营仪表盘", adminOnly: true },
      },
    ],
  },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach(async (to) => {
  const authed = Boolean(getAccessToken());

  if (to.meta.public) {
    // 已登录还去登录页 → 直接回工作台
    return authed ? { name: "workspace" } : true;
  }

  if (authed) {
    // 管理员专属页（如运营仪表盘）：非管理员直接回工作台。
    // 前端拦截只是为了体验，真正的权限边界在后端 require_admin（403）。
    if (to.meta.adminOnly && getCurrentUser()?.role !== "admin") {
      return { name: "workspace" };
    }
    return true;
  }

  // 没有访问令牌但可能有刷新令牌（刷新页面后）：尝试静默续期
  const restored = await refreshAccessToken();
  if (restored) return true;

  return { name: "login", query: { redirect: to.fullPath } };
});

router.afterEach((to) => {
  const title = typeof to.meta.title === "string" ? to.meta.title : "";
  document.title = title ? `${title} · くらし寝具 智能客服` : "くらし寝具 智能客服工作台";
});
