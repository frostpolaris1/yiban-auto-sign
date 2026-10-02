import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import CalendarPage from "./CalendarPage.vue";
import type { CalendarCtx } from "./types";

/* 签到日历页入口（/user/calendar 与 /my/calendar 共用）。

   顺序契约同其它页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
   本页当前零 Element Plus 组件，但**仍然引 EP 样式与主题桥**：`web/services/vue_assets.py`
   的 css 收集走"入口 + 依赖闭包"，而 vue 与 element-plus 目前合并在同一个 vendor chunk 里
   （manualChunks 的 vendor-vue 拆分未生效，见 docs/refactor/29 §15.6 登记项），
   EP 的 CSS/JS 无论如何都会随该 chunk 进本页——省掉这两行只会让本页与其它四页不一致，
   并在日后加 EP 组件时留下"组件无样式"的坑。

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
