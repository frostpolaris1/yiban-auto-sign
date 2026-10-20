/*
 * 早期引导（CSP 加固，工单 56ed）：主题 / 子路径前缀 / 版本兜底 / 图片回退 / 委托点击。
 *
 * 为什么外置：`script-src` 去掉 'unsafe-inline' 后，模板里的内联 <script> 块与
 * 内联事件属性（onclick / onerror）都会被浏览器拦掉。本文件承接原
 * `partials/theme_boot.html` 的四段内联脚本（主题、BASE、版本兜底）与模板里的
 * 事件属性（退出登录、更新日志、品牌图/备案图回退）。
 *
 * 为什么**不能**加 defer：主题必须在样式表之前落 `data-theme`，否则首屏会先按
 * 亮色绘制再切暗色（FOUC）。本文件在 <head> 内以同步脚本加载。
 *
 * 依赖的服务端下发（渲染进 <head> 的 meta）：
 *   <meta name="yiban-base" content="{{ request.script_root }}">
 *   <meta name="yiban-web-version" content="{{ web_version }}">
 * 不在脚本里拼 Jinja 表达式，故本文件是纯静态资源、可被长缓存（?v= 破缓存）。
 */
(function () {
  "use strict";

  function meta(name) {
    var el = document.querySelector('meta[name="' + name + '"]');
    return el ? (el.getAttribute("content") || "") : "";
  }

  // ① 主题：优先 localStorage 记忆；未设置时保持亮色（沿用历史默认，不跟随系统）。
  try {
    document.documentElement.setAttribute(
      "data-theme",
      localStorage.getItem("yiban-theme") === "dark" ? "dark" : "light"
    );
  } catch (e) {
    document.documentElement.setAttribute("data-theme", "light");
  }

  // ② 挂载前缀：全局 lexical 绑定（core.js 以 `typeof BASE === "string"` 读取）。
  //    子路径部署下非空；根路径/独立子域下为空串。所有跳转/静态资源/API 统一经它拼接。
  //    **必须挂到 window**：本文件其余逻辑在 IIFE 内，块级 const 不会成为跨脚本的全局；
  //    而 core.js 在另一个 <script> 里按裸名 `BASE` 读取（window 属性即全局绑定）。
  const BASE = meta("yiban-base");
  window.BASE = BASE;

  // ③ 版本失效兜底：页面版本与上次不同 → 强制刷新（?v= 绕过浏览器缓存）。
  (function () {
    try {
      var v = meta("yiban-web-version");
      if (!v) return;
      var prev = localStorage.getItem("web_version");
      localStorage.setItem("web_version", v);
      if (prev && prev !== v && !location.search.includes("v=" + v)) {
        location.replace(BASE + "/?v=" + v);
      }
    } catch (e) {}
  })();

  // ④ 图片回退（承接原 onerror 属性）。error 事件不冒泡，用捕获阶段监听；
  //    本脚本在 <head> 执行，早于正文里的 <img>，故失败事件不会漏。
  document.addEventListener(
    "error",
    function (ev) {
      var img = ev.target;
      if (!img || !img.getAttribute) return;
      // 品牌图：隐藏裂图、露出内联占位方块（#brand-fallback 由服务端渲染）
      if (img.getAttribute("data-img-fallback") === "brand") {
        img.style.display = "none";
        var fb = document.getElementById("brand-fallback");
        if (fb) fb.style.display = "grid";
        return;
      }
      // 备案图标：只隐藏裂图、不隐藏元素（保留 width/height 占位，行高不跳）
      if (img.hasAttribute("data-img-hide-on-error")) {
        img.style.visibility = "hidden";
      }
    },
    true
  );

  // ⑤ 委托点击（承接原 onclick="doLogout()" / onclick="openChangelog()"）。
  //    core.js 是 defer 加载，点击时 window.doLogout / openChangelog 已就绪。
  document.addEventListener("click", function (ev) {
    var el = ev.target && ev.target.closest
      ? ev.target.closest("[data-action]")
      : null;
    if (!el) return;
    var action = el.getAttribute("data-action");
    if (action === "logout" && typeof window.doLogout === "function") {
      ev.preventDefault();
      window.doLogout();
    } else if (action === "changelog" && typeof window.openChangelog === "function") {
      ev.preventDefault();
      window.openChangelog();
    }
  });
})();
