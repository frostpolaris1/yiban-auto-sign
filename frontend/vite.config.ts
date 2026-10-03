import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";

// 前端翻新构建配置（计划：docs/refactor/29-frontend-vue-refactor-plan.md §3）。
//
// base:'./' 是子路径部署的关键：构建产物里 chunk 间引用为相对路径，入口 <script>/<link>
// 的 URL 由 Flask 模板拼 request.script_root 前缀后，任意挂载点下都成立
// （BasePathMiddleware 自动探测形态不需要 Vite 感知前缀）。
//
// manifest:true 产出 ../web/static/vue/.vite/manifest.json，由
// web/services/vue_assets.py 启动时读一次并换算成模板可用的资产清单。
export default defineConfig({
  base: "./",
  plugins: [
    vue(),
    AutoImport({
      resolvers: [ElementPlusResolver()],
      dts: "src/auto-imports.d.ts",
    }),
    Components({
      resolvers: [ElementPlusResolver()],
      dts: "src/components.d.ts",
    }),
  ],
  build: {
    outDir: "../web/static/vue",
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      // 每页一个 HTML 入口（与「每页只加载自己的脚本」的既有纪律一致）；
      // manifest 键 = 入口 HTML 相对本目录的路径，Flask 侧按同一字符串取资产。
      input: {
        index: fileURLToPath(new URL("./index.html", import.meta.url)),
        audit: fileURLToPath(new URL("./audit.html", import.meta.url)),
        dashboard: fileURLToPath(new URL("./dashboard.html", import.meta.url)),
        logs: fileURLToPath(new URL("./logs.html", import.meta.url)),
        myaccounts: fileURLToPath(new URL("./myaccounts.html", import.meta.url)),
        calendar: fileURLToPath(new URL("./calendar.html", import.meta.url)),
        login: fileURLToPath(new URL("./login.html", import.meta.url)),
        users: fileURLToPath(new URL("./users.html", import.meta.url)),
        accounts: fileURLToPath(new URL("./accounts.html", import.meta.url)),
        settings: fileURLToPath(new URL("./settings.html", import.meta.url)),
      },
      output: {
        // vendor 独立成 chunk：多页入口共享同一份 vue / element-plus 按需集合，
        // 跨页命中浏览器长缓存（hash 文件名 + /static 长缓存策略）。
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (/[\\/]node_modules[\\/](vue|@vue)[\\/]/.test(id)) return "vendor-vue";
          if (/[\\/]node_modules[\\/](element-plus|@element-plus)[\\/]/.test(id)) {
            return "vendor-element-plus";
          }
          return "vendor-misc";
        },
      },
    },
  },
  server: {
    // 仅本地开发用：`npm run dev` 需要同时跑着 Flask（默认 127.0.0.1:8000，
    // 可用 YB_DEV_PROXY_TARGET 覆盖）。生产 / 测试不经过这里。
    proxy: {
      "/api": process.env.YB_DEV_PROXY_TARGET || "http://127.0.0.1:8000",
      "/static": process.env.YB_DEV_PROXY_TARGET || "http://127.0.0.1:8000",
    },
  },
});
