import { createApp } from "vue";
import { syncThemeWithShell } from "../lib/theme";
import CalendarPage from "./CalendarPage.vue";
import type { CalendarCtx } from "./types";

/* 签到日历页入口（/user/calendar 与 /my/calendar 共用）。

   顺序契约同其它页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
   本页零 Element Plus 组件，故**不再引 EP 的 CSS/主题桥**：2026-10-04 修好 vendor chunk
   拆分后，vue 运行时已独立成 vendor-vue chunk，本页依赖闭包不含 EP，"每个 Vue 页白载整份
   EP"的问题不复存在（详见 vite.config.ts 的 codeSplitting 注释）。页面自身的暗色令牌来自
   全局 app.css，与 EP 无关；日后本页真加 EP 组件时，unplugin 按需解析会自动带回组件样式，
   再补 `element-plus/theme-chalk/dark/css-vars.css` 与 `../styles/ep-theme.css` 两行即可。

   状态载荷读 `window.YB_CALENDAR_STATE`：由服务端内联渲染在正文里（**非 defer**，先于本模块
   执行），是"显示表单一源"（yiban.status.DISPLAY）的页面侧入口。 */

syncThemeWithShell();

const host = document.getElementById("vue-calendar-app");
const rawRole = host?.dataset.role;
const role: "user" | "admin" = rawRole === "admin" ? "admin" : "user";
const ctx = (window as { YB_CALENDAR_STATE?: CalendarCtx }).YB_CALENDAR_STATE ?? {
  by_code: {},
  by_symbol: {},
  day_off: null,
};

createApp(CalendarPage, {
  role,
  denyHref: host?.dataset.denyHref ?? "",
  emptyHref: host?.dataset.emptyHref ?? "",
  ctx,
}).mount("#vue-calendar-app");
