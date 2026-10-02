import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import Audit from "./Audit.vue";

// 顺序契约同试点页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
syncThemeWithShell();
createApp(Audit).mount("#vue-audit-app");
