import { createApp, h, type Component } from "vue";
import { ElConfigProvider } from "element-plus/es";
import zhCn from "element-plus/es/locale/lang/zh-cn";

/* 用到 Element Plus 的页面入口**统一装配**：外层套 `el-config-provider`（zh-cn），
   使 EP 组件内部文案与弹层（time-picker 面板、分页、select 空态……）走中文。

   为什么不用 `app.use(ElementPlus, { locale })`：本仓 EP 组件由 unplugin-vue-components
   按需解析，从不整体 `app.use(ElementPlus)`（那会把整库拉进 bundle，与按需装配相悖）。
   `el-config-provider` 是官方在按需用法下注入 locale 的方式，且它只 provide/inject、
   不产生额外 DOM（渲染一个 Fragment），故对既有结构与 e2e 零影响。

   未知/缺省时 EP 回落英文——这里显式给中文，避免"组件是中文壳、弹层是英文"的割裂。
   校准页（calendar/dashboard/login）零 EP 组件，不走本函数。 */
export function mountEpPage(
  root: Component,
  props: Record<string, unknown> | null,
  selector: string,
) {
  createApp({
    render: () => h(ElConfigProvider, { locale: zhCn }, { default: () => h(root, props ?? {}) }),
  }).mount(selector);
}
