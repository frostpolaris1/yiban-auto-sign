import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import DashboardPage from "./DashboardPage.vue";

/* 数据看板页入口（管理端 /data/dashboard）。

   顺序契约同其它页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
   本页的服务端载荷只有一件——热力图脚注基线文案（`window.YB_DASHBOARD_STATE`，
   由 `web/templates/pages/data_dashboard.html` 内联渲染、**非 defer**，先于本模块执行）：
   它描述的是"数据不含探针"这条后端语义，故留在服务端而非组件里硬编码；其余数据全客户端拉取。

   Chart.js 由模板以 defer 先行加载（vendor UMD，v4.5.1）：本模块执行时 `window.Chart`
   已就位（defer 与 module 脚本同属"解析完成后按文档顺序执行"的队列）。 */

syncThemeWithShell();

const host = window as { YB_DASHBOARD_STATE?: { cal_note?: string } };
createApp(DashboardPage, { calNote: host.YB_DASHBOARD_STATE?.cal_note ?? "" }).mount("#vue-dashboard-app");
