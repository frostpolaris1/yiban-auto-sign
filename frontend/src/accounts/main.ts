import { mountEpPage } from "../lib/ep-mount";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import Accounts from "./Accounts.vue";

/* 账号管理页入口（管理端 /work/accounts）。

   顺序契约同其它页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
   本页用到 EP 的 dialog / select / dropdown（窄屏行菜单），由 unplugin 按需解析；
   挂载经 mountEpPage，统一套 el-config-provider(zh-cn)。 */

syncThemeWithShell();
mountEpPage(Accounts, null, "#vue-accounts-app");
