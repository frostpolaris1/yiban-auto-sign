import { defineConfig } from "vitest/config";

// 前端单测管道（P1 引入）：纯函数与组件的单元测试，与 Python 契约测试互补——
// Python 侧钉「页面能渲染、资产在盘、守卫正确」，这里钉「查询构造与翻页状态机」。
// environment 用 happy-dom：纯函数测试同样可跑，后续组件挂载测试无需换配置。
export default defineConfig({
  // css:true：ep-confirm 直接引 ElMessageBox 的样式入口（element-plus/es 的 style/css
  // 链），node_modules 里的 .css 须交给 Vite 管线解析，否则 vitest 以「未知扩展名」
  // 报红。happy-dom 下样式规则只进 document、不影响断言。
  css: true,
  test: {
    include: ["src/**/*.spec.ts"],
    environment: "happy-dom",
  },
});
