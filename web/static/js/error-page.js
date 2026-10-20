/*
 * 错误页「返回上一页」（CSP 加固，工单 56ed：原 error.html 内联脚本外置）。
 *
 * 只在确有历史记录时显示按钮：直接打开链接时它什么也做不了，显形反而误点。
 * 以 defer 加载，执行时 #err-back 已在 DOM 中。
 */
(function () {
  "use strict";
  var btn = document.getElementById("err-back");
  if (!btn || window.history.length <= 1) return;
  btn.hidden = false;
  btn.addEventListener("click", function () {
    window.history.back();
  });
})();
