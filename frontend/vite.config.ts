import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发期通过代理把 /api 转发到 FastAPI 后端，避免跨域；
// 生产环境由 Nginx 做同样的反向代理（见 frontend/README.md）。
// 注意：本机 8000 端口常被其他项目占用，本项目统一约定使用 8002。
const BACKEND = process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8002";

// 构建版本标识（注入到界面侧栏，用于一眼确认浏览器跑的是不是新构建）
const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf-8")) as {
  version?: string;
};
const buildTime = new Date().toISOString().slice(0, 16).replace("T", " ");
const __BUILD_INFO__ = `v${pkg.version ?? "?"} · build ${buildTime}`;

export default defineConfig({
  plugins: [vue()],
  define: { __BUILD_INFO__: JSON.stringify(__BUILD_INFO__) },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/healthz": { target: BACKEND, changeOrigin: true },
      "/readyz": { target: BACKEND, changeOrigin: true },
    },
  },
});
