import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import Users from "./Users.vue";

/* 用户管理页入口（管理端 /work/users）。

   顺序契约同其它页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
   行菜单用 `el-dropdown`（legacy 借共享 row-menu.js），故本页确实用到 EP 组件。 */

syncThemeWithShell();
createApp(Users).mount("#vue-users-app");
