import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import Pilot from "./Pilot.vue";

// 顺序契约：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
syncThemeWithShell();
createApp(Pilot).mount("#vue-pilot-app");
