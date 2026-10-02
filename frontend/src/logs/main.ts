import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import Logs from "./Logs.vue";

// 顺序契约同其他页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
syncThemeWithShell();
createApp(Logs).mount("#vue-logs-app");
