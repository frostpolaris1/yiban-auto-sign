import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import SettingsPage from "./SettingsPage.vue";

/* 系统设置页入口（管理端 /work/settings）。

   顺序契约同其它页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
   本页用到 EP 的 select / slider / time-picker / checkbox / switch / dialog / dropdown，
   由 unplugin 按需解析（不手工 import，避免与组件解析器重复登记）。 */

syncThemeWithShell();
createApp(SettingsPage).mount("#vue-settings-app");
