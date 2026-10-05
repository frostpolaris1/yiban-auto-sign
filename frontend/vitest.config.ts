import { defineConfig } from "vitest/config";

// 前端单测管道（P1 引入）：纯函数与组件的单元测试，与 Python 契约测试互补——
// Python 侧钉「页面能渲染、资产在盘、守卫正确」，这里钉「查询构造与翻页状态机」。
// environment 用 happy-dom：纯函数测试同样可跑，后续组件挂载测试无需换配置。
export default defineConfig({
  test: {
    include: ["src/**/*.spec.ts"],
    environment: "happy-dom",
  },
});
