import { mountEpPage } from "../lib/ep-mount";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import Logs from "./Logs.vue";

// 顺序契约同其他页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载（挂载经
// mountEpPage，统一套 el-config-provider(zh-cn)）。
syncThemeWithShell();
mountEpPage(Logs, null, "#vue-logs-app");
