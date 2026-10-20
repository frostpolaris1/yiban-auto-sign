/*
 * 首屏加载指示（CSP 加固，工单 56ed：原三套 layout 里的内联脚本外置）。
 *
 * 120ms 内就绪则完全不出现（快加载不闪）；core.js 就绪或 window.load 后淡出。
 * 服务端已渲染正文，故不遮内容，只在顶部提示「样式/脚本仍在就绪」。
 * 以 defer 加载，执行时 #boot-loader 已在 DOM 中。
 */
(function () {
  "use strict";
  var el = document.getElementById("boot-loader");
  if (!el) return;
  var t1 = setTimeout(function () {
    if (!el.classList.contains("is-ready")) el.classList.add("is-visible");
  }, 120);
  function done() {
    clearTimeout(t1);
    el.classList.remove("is-visible");
    el.classList.add("is-ready");
  }
  window.addEventListener("load", done);
  var t2 = setInterval(function () {
    if (window.YB && window.YB.__ready) {
      clearInterval(t2);
      done();
    }
  }, 60);
  setTimeout(function () {
    clearInterval(t2);
    done();
  }, 5000); // 兜底：异常时不常驻
})();
