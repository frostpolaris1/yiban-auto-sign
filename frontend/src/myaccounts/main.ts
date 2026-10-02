import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import MyAccounts from "./MyAccounts.vue";

// 一个入口服务两页（`/user/account` 与 `/my/account`）：两页**共用同一实现**（对齐 legacy 的
// 共享 partial + 编排器 components/my-accounts-page.js）。页面差异是**配置**而非代码——
// 变体（user/admin）与日历链接由服务端渲染在挂载点的 data-* 上，故不必复制一份 bundle。
// 这与「每页只加载自己的脚本」不冲突：两页本来就是同一份脚本。
syncThemeWithShell();

const host = document.getElementById("vue-myaccounts-app");
const raw = host?.dataset.variant;
const variant: "user" | "admin" = raw === "admin" ? "admin" : "user";
createApp(MyAccounts, { variant, calendarHref: host?.dataset.calendarHref ?? "" }).mount("#vue-myaccounts-app");
