import { mountEpPage } from "../lib/ep-mount";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import Users from "./Users.vue";

/* 用户管理页入口（管理端 /work/users）。

   顺序契约同其它页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
   行菜单用 `el-dropdown`（不再借共享 row-menu.js），故本页确实用到 EP 组件；
   挂载经 mountEpPage，统一套 el-config-provider(zh-cn)。 */

syncThemeWithShell();
mountEpPage(Users, null, "#vue-users-app");
