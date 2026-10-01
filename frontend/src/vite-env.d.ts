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
  }
}

declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>;
  export default component;
}

export {};
