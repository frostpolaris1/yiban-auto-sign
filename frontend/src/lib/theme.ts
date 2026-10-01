/**
 * 主题同步（单方向：外壳为事实源，Vue 只跟随）。
 *
 * 本项目外壳的深浅色由 partials/theme_boot.html 在绘制前写 html[data-theme]，
 * core.js 的 applyTheme/toggleTheme 维护并派发 `yiban:theme`（CustomEvent，目标是
 * **document** 而非 window —— core.js:882）。Element Plus 的暗色走 html.dark。
 * 这里把两者钉在一起：读一次 + 监听一次，此后任何换肤入口（侧栏按钮 / 试点页
 * 开关，后者经 window.YB.applyTheme 走既有机器）都会自动传导到 EP 组件。
 */
export function syncThemeWithShell(): void {
  const apply = (): void => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    document.documentElement.classList.toggle("dark", dark);
  };
  apply();
  document.addEventListener("yiban:theme", apply);
}
