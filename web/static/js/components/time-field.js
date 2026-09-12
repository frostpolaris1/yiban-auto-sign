/* 时间字段（时 / 分两个模板 .select，取代原生 input[type=time]）。

   挂载到 window.YB.timeField；classic script，公开面：
     mount()        初始化文档内所有 [data-time-field]（幂等）
     set(id, hhmm)  写入 "HH:MM"：同时更新两个 select 与隐藏 input
     read(id)       读取隐藏 input 的 "HH:MM"

   为什么不用原生 input[type=time]：展开的选时列表由 UA 渲染，高亮色、选中黑框、
   圆角、行高都不可控（实测高亮 #0075FF 与项目主色 #2563EB 不同、选中格 3px 黑框、方角），
   且跨浏览器/跨系统表现各异 —— 与设计系统无法一致。

   契约：**隐藏 input 保留原 id 与 "HH:MM" 值**，故读取方（`$(id).value`）无需改动；
   只有写入方需要改用 `YB.timeField.set(id, v)`（写隐藏值不会更新两个 select）。
   select 变更时同步回隐藏 input 并派发 change，故既有 change 监听器照常工作。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function rootOf(id) { return document.querySelector('[data-time-field="' + id + '"]'); }
  function parts(root) {
    return {
      h: root.querySelector('[data-part="hour"]'),
      m: root.querySelector('[data-part="minute"]'),
      input: root.querySelector("input[type=hidden]")
    };
  }
  function fill(sel, to) {
    for (var v = 0; v <= to; v++) {
      sel.appendChild(YB.el("option", { value: pad(v), text: pad(v) }));
    }
  }

  function set(id, hhmm) {
    var root = rootOf(id);
    if (!root) return;
    var p = parts(root);
    var v = String(hhmm == null ? "" : hhmm).trim().slice(0, 5);
    if (!/^\d{2}:\d{2}$/.test(v)) return;
    if (p.h) p.h.value = v.slice(0, 2);
    if (p.m) p.m.value = v.slice(3, 5);
    if (p.input) p.input.value = v;
  }

  function read(id) {
    var n = document.getElementById(id);
    return n ? n.value : "";
  }

  function sync(root) {
    var p = parts(root);
    if (!p.input) return;
    p.input.value = (p.h && p.h.value ? p.h.value : "00") + ":" + (p.m && p.m.value ? p.m.value : "00");
    // 沿用既有 change 监听器（它们绑在隐藏 input 上），无需改调用方
    p.input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function mount() {
    [].forEach.call(document.querySelectorAll("[data-time-field]"), function (root) {
      var p = parts(root);
      if (!p.h || !p.m || p.h.options.length) return;   // 已初始化或结构不全
      fill(p.h, 23);
      fill(p.m, 59);
      var init = p.input && /^\d{2}:\d{2}$/.test(p.input.value) ? p.input.value : "00:00";
      p.h.value = init.slice(0, 2);
      p.m.value = init.slice(3, 5);
      p.h.addEventListener("change", function () { sync(root); });
      p.m.addEventListener("change", function () { sync(root); });
    });
  }

  YB.timeField = { mount: mount, set: set, read: read };
})();
