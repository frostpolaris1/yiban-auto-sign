/// <reference types="vite/client" />

/** 外壳注入的全局（core.js / theme_boot）—— 只声明 Vue 线实际读到的最小面。 */
declare global {
  interface Window {
    /** 子路径部署前缀，partials/theme_boot.html 绘制前写入 */
    BASE?: unknown;
    /** 外壳行为层（web/static/js/core.js）；试点页只用 applyTheme 走既有换肤机器 */
    YB?: {
      applyTheme?: (theme: string, persist?: boolean) => void;
      currentTheme?: () => string;
    };
    /**
     * 日历页的状态显示载荷，由服务端内联渲染进正文（**非 defer**，先于模块脚本执行）。
     * 形状见 src/calendar/types.ts 的 CalendarCtx；唯一事实源是 yiban.status.DISPLAY。
     */
    YB_CALENDAR_STATE?: unknown;
  }
}

declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>;
  export default component;
}

export {};
