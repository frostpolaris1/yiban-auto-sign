import { createApp, h, type Component } from "vue";
import { ElConfigProvider } from "element-plus/es";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import "element-plus/es/components/message-box/style/css";
import { registerEpConfirm } from "./ep-confirm";

/* 用到 Element Plus 的页面入口**统一装配**：外层套 `el-config-provider`（zh-cn），
   使 EP 组件内部文案与弹层（time-picker 面板、分页、select 空态……）走中文。

   为什么不用 `app.use(ElementPlus, { locale })`：本仓 EP 组件由 unplugin-vue-components
   按需解析，从不整体 `app.use(ElementPlus)`（那会把整库拉进 bundle，与按需装配相悖）。
   `el-config-provider` 是官方在按需用法下注入 locale 的方式，且它只 provide/inject、
   不产生额外 DOM（渲染一个 Fragment），故对既有结构与 e2e 零影响。

   未知/缺省时 EP 回落英文——这里显式给中文，避免"组件是中文壳、弹层是英文"的割裂。
   校准页（calendar/dashboard/login）零 EP 组件，不走本函数。

   **message-box 样式**在此引：确认族弹窗已换成 `ElMessageBox`（uz7w），它是服务式
   调用、不经模板解析，故 unplugin-vue-components 不会为它补样式；装配入口是唯一
   能保证「用到 EP 的页面必有盒子样式」的位置。样式不放 ep-confirm.ts —— 那里被
   vitest 直接 import，node 原生 ESM 不认 `.css`（见该文件说明）。 */
export function mountEpPage(
  root: Component,
  props: Record<string, unknown> | null,
  selector: string,
) {
  // 注册到外壳探测面：core.js 的 confirmDialog 命中后走 EP，无 EP 的页面仍落旧实现。
  registerEpConfirm();
  createApp({
    render: () => h(ElConfigProvider, { locale: zhCn }, { default: () => h(root, props ?? {}) }),
  }).mount(selector);
}
export { registerEpConfirm };
