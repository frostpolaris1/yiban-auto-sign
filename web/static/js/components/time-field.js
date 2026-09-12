/* 滚动时间轮（时 / 分），取代原生 input[type=time] 与 <select>。

   挂载到 window.YB.timeField；classic script，公开面：
     mount()        初始化文档内所有 [data-time-field]（幂等）
     set(id, hhmm)  写入 "HH:MM"：同步两个轮子的位置与隐藏 input
     read(id)       读取隐藏 input 的 "HH:MM"

   为什么不用原生控件：
     · input[type=time] 的选时列表由 UA 渲染，高亮色/黑框/圆角不可控（实测高亮 #0075FF
       与项目主色 #2563EB 不同）；
     · <select> 的下拉面板同样是 UA 渲染，无法与设计系统一致，且选"分钟"要滚 60 项。
   改用滚动轮：上下滑动选择（CSS scroll-snap 居中），中间行为当前值，键盘上下/Home/End 可用。

   契约：**隐藏 input 保留原 id 与 "HH:MM" 值**，故读取方（`$(id).value`）无需改动；
   写入方需改用 `YB.timeField.set(id, v)`（写隐藏值不会移动轮子）。
   用户滚动后同步回隐藏 input 并派发 change（程序化 set 不派发，避免误标脏）。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ITEM_H = 44;          // 与 .wheel-item 的行高一致（滚动定位按它换算）
  var SETTLE_MS = 90;       // 滚动停止判定

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function rootOf(id) { return document.querySelector('[data-time-field="' + id + '"]'); }
  function hiddenOf(root) { return root.querySelector("input[type=hidden]"); }

  function buildWheel(count, label) {
    var wheel = YB.el("div", { class: "wheel", tabindex: "0", role: "listbox", "aria-label": label });
    wheel.appendChild(YB.el("div", { class: "wheel-pad", "aria-hidden": "true" }));
    for (var v = 0; v < count; v++) {
      wheel.appendChild(YB.el("div", {
        class: "wheel-item", role: "option", "aria-selected": "false",
        "data-v": pad(v), text: pad(v)
      }));
    }
    wheel.appendChild(YB.el("div", { class: "wheel-pad", "aria-hidden": "true" }));
    return wheel;
  }

  function items(wheel) { return wheel.querySelectorAll(".wheel-item"); }
  function indexOf(wheel) {
    var i = Math.round(wheel.scrollTop / ITEM_H);
    var n = items(wheel).length;
    return Math.max(0, Math.min(n - 1, i));
  }
  function markSel(wheel, i) {
    var list = items(wheel);
    for (var k = 0; k < list.length; k++) {
      var on = k === i;
      list[k].classList.toggle("is-sel", on);
      list[k].setAttribute("aria-selected", on ? "true" : "false");
    }
  }
  function scrollToIndex(wheel, i, smooth) {
    var top = i * ITEM_H;
    if (smooth) wheel.scrollTo({ top: top, behavior: "smooth" });
    else wheel.scrollTop = top;
    markSel(wheel, i);
  }
  function valueOf(root) {
    var wheel = root.querySelector(".wheel");
    var h = wheel ? items(wheel)[indexOf(wheel)] : null;
    var m = wheel ? items(root.querySelectorAll(".wheel")[1])[indexOf(root.querySelectorAll(".wheel")[1])] : null;
    return {
      h: h ? h.getAttribute("data-v") : "00",
      m: m ? m.getAttribute("data-v") : "00"
    };
  }

  // 用户滚动 → 吸附到位后回写隐藏 input 并派发 change（沿用既有 change 监听器）
  function bindScroll(root, wheel, syncing) {
    var timer = null;
    wheel.addEventListener("scroll", function () {
      markSel(wheel, indexOf(wheel));
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        if (syncing.on) return;                       // 程序化 set 期间不派发
        var input = hiddenOf(root);
        if (!input) return;
        var v = valueOf(root);
        var next = v.h + ":" + v.m;
        if (input.value === next) return;
        input.value = next;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }, SETTLE_MS);
    });
    wheel.addEventListener("keydown", function (e) {
      var i = indexOf(wheel), n = items(wheel).length, to = null;
      if (e.key === "ArrowUp") to = i - 1;
      else if (e.key === "ArrowDown") to = i + 1;
      else if (e.key === "PageUp") to = i - 5;
      else if (e.key === "PageDown") to = i + 5;
      else if (e.key === "Home") to = 0;
      else if (e.key === "End") to = n - 1;
      if (to == null) return;
      e.preventDefault();
      scrollToIndex(wheel, Math.max(0, Math.min(n - 1, to)), true);
    });
    wheel.addEventListener("click", function (e) {
      var it = e.target.closest ? e.target.closest(".wheel-item") : null;
      if (!it) return;
      var list = items(wheel);
      scrollToIndex(wheel, Array.prototype.indexOf.call(list, it), true);
    });
  }

  function set(id, hhmm) {
    var root = rootOf(id);
    if (!root) return;
    var v = String(hhmm == null ? "" : hhmm).trim().slice(0, 5);
    if (!/^\d{2}:\d{2}$/.test(v)) return;
    var wheels = root.querySelectorAll(".wheel");
    if (wheels.length < 2) return;
    var sync = root.__syncing || (root.__syncing = { on: false });
    sync.on = true;
    scrollToIndex(wheels[0], parseInt(v.slice(0, 2), 10), false);
    scrollToIndex(wheels[1], parseInt(v.slice(3, 5), 10), false);
    var input = hiddenOf(root);
    if (input) input.value = v;
    setTimeout(function () { sync.on = false; }, SETTLE_MS + 40);
  }

  function read(id) {
    var n = document.getElementById(id);
    return n ? n.value : "";
  }

  function mount() {
    [].forEach.call(document.querySelectorAll("[data-time-field]"), function (root) {
      if (root.querySelector(".wheel")) return;        // 已初始化
      var sync = root.__syncing || (root.__syncing = { on: false });
      var label = root.getAttribute("aria-label") || "时间";
      var hw = buildWheel(24, label + " · 小时");
      var mw = buildWheel(60, label + " · 分钟");
      root.appendChild(hw);
      root.appendChild(YB.el("span", { class: "wheel-sep", "aria-hidden": "true", text: ":" }));
      root.appendChild(mw);
      bindScroll(root, hw, sync);
      bindScroll(root, mw, sync);
      var input = hiddenOf(root);
      var init = input && /^\d{2}:\d{2}$/.test(input.value) ? input.value : "00:00";
      sync.on = true;
      scrollToIndex(hw, parseInt(init.slice(0, 2), 10), false);
      scrollToIndex(mw, parseInt(init.slice(3, 5), 10), false);
      setTimeout(function () { sync.on = false; }, SETTLE_MS + 40);
    });
  }

  YB.timeField = { mount: mount, set: set, read: read };
})();
