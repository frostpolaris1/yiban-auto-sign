import { defineConfig } from "vitest/config";

// 前端单测管道（P1 引入）：纯函数与组件的单元测试，与 Python 契约测试互补——
// Python 侧钉「页面能渲染、资产在盘、守卫正确」，这里钉「查询构造与翻页状态机」。
// environment 用 happy-dom：纯函数测试同样可跑，组件挂载测试也无需换配置。
//
// 不引 EP 的样式入口：`element-plus/es/**/style/css` 会拉 `theme-chalk/*.css`，
// node 原生 ESM 不认 `.css`（`Unknown file extension ".css"`）。样式只在页面装配
// 入口（lib/ep-mount.ts）引，那是 Vite 的构建图；单测不需要任何 CSS。
export default defineConfig({
  test: {
    include: ["src/**/*.spec.ts"],
    environment: "happy-dom",
  },
});
