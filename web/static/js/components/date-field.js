/* 模板化日期选择（自研月历弹层，取代原生 <input type=date> 的 UA 弹出层）。

   挂载到 window.YB.dateField；classic script，公开面：
     mount()                初始化文档内所有 [data-date-field]（幂等）
     set(id, value)         写入日期（YYYY-MM-DD），同步原生 input 与触发器文案
     read(id)               读取原生 input 的值
     setDisabled(id, on)    启用/禁用（整组置灰、触发键不可点）

   为什么不用原生 input[type=date] 的弹出层：面板由 UA 渲染（Windows 下是系统日历 +
   系统投影 + 不可控高亮），月份切换与「今天」入口都不可定制，与设计系统不一致。
   本项目已用同一理由替换了原生 select / range / time（见 select-field / range-field /
   time-field），日期是这批控件里的最后一处。

   契约与另外三件套同口径：**模板里的原生 input[type=date] 保留原 id 与值**，
   读取方（`$(id).value`）零改动；写入方走 set()（直接写 .value 不会更新触发器文案）。
   `mount()` 才给原生控件打 is-enhanced 标记并隐藏它 —— 脚本未加载时它仍是一个可用的
   原生日期输入框（渐进增强，功能不丢）。用户选择时回写原生 input 并派发 change
   （程序化 set 不派发，避免把回填误标成用户改动）。

   可访问性：触发器是 button + aria-haspopup="dialog" + aria-expanded；弹层 role="dialog"
   + aria-label（默认「选择日期」）；月历按周一起始、固定 6 行（换月不跳高）；
   ←/→ 前后一天、↑/↓ 前后一周、Home/End 本周首末、PageUp/PageDown 前后一月、
   Enter/Space 选定、Esc 收起并归还焦点；焦点在弹层内用 preventScroll 定位，
   避免"打开就把整页上拽"。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var WEEK = ["一", "二", "三", "四", "五", "六", "日"];
  var ROWS = 6;                 // 固定 6 行（42 格）：任意月份等高，换月不跳
  var uid = 0;
  var globalBound = false;

  function svg(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  function roots() { return [].slice.call(document.querySelectorAll("[data-date-field]")); }
  function rootOf(id) { return document.querySelector('[data-date-field="' + id + '"]'); }
  function inputOf(root) { return root.querySelector("input[type=date]"); }
  function triggerOf(root) { return root.querySelector(".date-trigger"); }
  function popOf(root) { return root.querySelector(".date-pop"); }
  function pad2(n) { return ("0" + n).slice(-2); }
  function fmt(y, m, d) { return y + "-" + pad2(m) + "-" + pad2(d); }
  function todayStr() {
    var d = new Date();
    return fmt(d.getFullYear(), d.getMonth() + 1, d.getDate());
  }
  function parse(s) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(s || ""))) return null;
    var y = +String(s).slice(0, 4), m = +String(s).slice(5, 7), d = +String(s).slice(8, 10);
    var dt = new Date(y, m - 1, d);
    // 逐项比对而不是 toISOString 回环：后者按 UTC 折算，UTC+8 下会把日期回退一天
    return (dt.getFullYear() === y && dt.getMonth() === m - 1 && dt.getDate() === d) ? dt : null;
  }
  function daysInMonth(y, m) { return new Date(y, m, 0).getDate(); }
  // 周一起始的列偏移（0=周一）
  function leadOf(y, m) { return (new Date(y, m - 1, 1).getDay() + 6) % 7; }

  // 面板内聚焦一律 preventScroll：默认的"聚焦即滚动"会把整页拽一下
  function focusNoScroll(node) {
    if (!node) return;
    try { node.focus({ preventScroll: true }); } catch (e) { node.focus(); }
  }

  // 触发器要能被读屏关联到字段标签：增强后原生 input 已 aria-hidden，
  // 就近取 <label for="ID"> 挂到触发器上（等价 select-field 的 labelFor）
  function labelFor(root, input) {
    var trigger = triggerOf(root);
    if (!trigger || !input || !input.id || !root.parentNode) return;
    var label = root.parentNode.querySelector('label[for="' + input.id + '"]');
    if (!label) return;
    if (!label.id) { uid += 1; label.id = "df-label-" + uid; }
    trigger.setAttribute("aria-labelledby", label.id);
  }

  function viewOf(root) {
    var v = (root.getAttribute("data-view") || "").split("-");
    if (v.length === 3) return { y: +v[0], m: +v[1] };
    var dt = parse(inputOf(root) && inputOf(root).value) || new Date();
    return { y: dt.getFullYear(), m: dt.getMonth() + 1 };
  }
  function setView(root, y, m) {
    if (m < 1) { m = 12; y -= 1; }
    if (m > 12) { m = 1; y += 1; }
    root.setAttribute("data-view", y + "-" + pad2(m));
  }

  function paint(root) {
    var input = inputOf(root);
    var text = triggerOf(root) && triggerOf(root).querySelector(".date-trigger-text");
    var v = input ? input.value : "";
    if (text) text.textContent = v || root.getAttribute("data-placeholder") || "选择日期";
    root.classList.toggle("is-empty", !v);
  }

  // ---- 弹层内容 ----
  function dayButton(root, y, m, d) {
    var date = fmt(y, m, d);
    var input = inputOf(root);
    var sel = input && input.value === date;
    var isToday = date === todayStr();
    var btn = YB.el("button", {
      type: "button", class: "date-day", "data-date": date, text: String(d),
      "aria-label": date + (isToday ? "，今天" : "") + (sel ? "，已选中" : ""),
      "aria-pressed": sel ? "true" : "false", tabindex: "-1"
    });
    if (sel) btn.classList.add("is-sel");
    if (isToday) btn.classList.add("is-today");
    return btn;
  }

  function renderPop(root) {
    var pop = popOf(root);
    if (!pop) return;
    var v = viewOf(root);
    var label = pop.querySelector(".date-month");
    if (label) label.textContent = v.y + " 年 " + v.m + " 月";
    var grid = pop.querySelector(".date-grid");
    if (!grid) return;
    while (grid.firstChild) grid.removeChild(grid.firstChild);
    var lead = leadOf(v.y, v.m);
    var days = daysInMonth(v.y, v.m);
    var cells = 0, d;
    for (d = 0; d < lead; d++) { grid.appendChild(YB.el("span", { class: "date-blank" })); cells += 1; }
    for (d = 1; d <= days; d++) { grid.appendChild(dayButton(root, v.y, v.m, d)); cells += 1; }
    while (cells < ROWS * 7) { grid.appendChild(YB.el("span", { class: "date-blank" })); cells += 1; }
  }

  // 当前应聚焦的日：已选 → 今天（当月）→ 当月 1 号
  function focusTarget(root) {
    var pop = popOf(root);
    if (!pop) return null;
    var v = viewOf(root);
    var input = inputOf(root);
    var want = input && input.value && input.value.indexOf(v.y + "-" + pad2(v.m)) === 0
      ? input.value
      : (todayStr().indexOf(v.y + "-" + pad2(v.m)) === 0 ? todayStr() : fmt(v.y, v.m, 1));
    return pop.querySelector('.date-day[data-date="' + want + '"]') || pop.querySelector(".date-day");
  }

  function close(root, back) {
    var pop = popOf(root), trigger = triggerOf(root);
    if (!pop || pop.hidden) return;
    pop.hidden = true;
    root.classList.remove("is-open");
    if (trigger) {
      trigger.setAttribute("aria-expanded", "false");
      if (back) trigger.focus();
    }
  }

  // 弹层钳制到视口内。算法要"目标位置 − 锚点位置"一次算准：
  // 弹层是 .date-field 的绝对定位子元素，left 相对该字段；若先换锚（右对齐）再按旧位置
  // 补偏移，两者会打架（实测越界方向从左侧换到右侧）。故只保留 left 一种锚定方式。
  var POP_PAD = 8;
  function clampPop(root) {
    var pop = popOf(root);
    if (!pop) return;
    pop.style.left = "";
    var vw = document.documentElement.clientWidth || window.innerWidth || 0;
    var fb = root.getBoundingClientRect();
    var w = pop.offsetWidth || 272;
    var want = Math.min(Math.max(fb.left, POP_PAD), Math.max(POP_PAD, vw - POP_PAD - w));
    pop.style.left = Math.round(want - fb.left) + "px";
  }

  function open(root) {
    var pop = popOf(root), trigger = triggerOf(root);
    if (!pop || !trigger || trigger.disabled) return;
    roots().forEach(function (r) { if (r !== root) close(r, false); });
    setView(root, viewOf(root).y, viewOf(root).m);
    renderPop(root);
    pop.hidden = false;
    root.classList.add("is-open");
    trigger.setAttribute("aria-expanded", "true");
    // 必须在 hidden=false 之后量宽（display:none 下 offsetWidth 为 0）
    clampPop(root);
    focusNoScroll(focusTarget(root));
  }

  function commit(root, date) {
    var input = inputOf(root);
    if (!input) return;
    var changed = input.value !== date;
    input.value = date;
    paint(root);
    close(root, true);
    if (changed) input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function shiftFocus(root, delta, mode) {
    var pop = popOf(root);
    if (!pop) return;
    var cur = document.activeElement && document.activeElement.classList
      && document.activeElement.classList.contains("date-day") ? document.activeElement : null;
    var base = parse(cur && cur.getAttribute("data-date")) || parse(inputOf(root) && inputOf(root).value) || new Date();
    var next = new Date(base.getFullYear(), base.getMonth(), base.getDate());
    if (mode === "month") next.setMonth(next.getMonth() + delta);
    else next.setDate(next.getDate() + delta * (mode === "week" ? 7 : 1));
    var v = viewOf(root);
    if (next.getFullYear() !== v.y || next.getMonth() + 1 !== v.m) {
      setView(root, next.getFullYear(), next.getMonth() + 1);
      renderPop(root);
    }
    var node = pop.querySelector('.date-day[data-date="' + fmt(next.getFullYear(), next.getMonth() + 1, next.getDate()) + '"]');
    focusNoScroll(node);
  }

  function bindPop(root, pop) {
    pop.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var day = t.closest(".date-day");
      if (day) { commit(root, day.getAttribute("data-date")); return; }
      var nav = t.closest("[data-date-nav]");
      if (nav) { setView(root, viewOf(root).y, viewOf(root).m + Number(nav.getAttribute("data-date-nav"))); renderPop(root); return; }
      if (t.closest("[data-date-today]")) { commit(root, todayStr()); }
    });
    pop.addEventListener("keydown", function (e) {
      var key = e.key;
      if (key === "Escape") { e.preventDefault(); e.stopPropagation(); close(root, true); return; }
      if (key === "Enter" || key === " ") {
        var day = e.target.closest && e.target.closest(".date-day");
        if (day) { e.preventDefault(); commit(root, day.getAttribute("data-date")); }
        return;
      }
      var handled = true;
      if (key === "ArrowLeft") shiftFocus(root, -1, "day");
      else if (key === "ArrowRight") shiftFocus(root, 1, "day");
      else if (key === "ArrowUp") shiftFocus(root, -1, "week");
      else if (key === "ArrowDown") shiftFocus(root, 1, "week");
      else if (key === "PageUp") shiftFocus(root, -1, "month");
      else if (key === "PageDown") shiftFocus(root, 1, "month");
      else if (key === "Home" || key === "End") {
        var cur = e.target.closest && e.target.closest(".date-day");
        var dt = parse(cur && cur.getAttribute("data-date"));
        if (!dt) return;
        var wd = (dt.getDay() + 6) % 7;                       // 周一起始
        shiftFocus(root, key === "Home" ? -wd : 6 - wd, "day");
      } else handled = false;
      if (handled) e.preventDefault();
    });
  }

  function build(root) {
    var input = inputOf(root);
    if (!input || triggerOf(root)) return;
    root.classList.add("is-enhanced");
    // 隐藏原生控件但保留它的值与 id 契约；tabindex/aria-hidden 让读屏只认触发器
    input.setAttribute("tabindex", "-1");
    input.setAttribute("aria-hidden", "true");

    var label = root.getAttribute("data-title") || "选择日期";
    uid += 1;
    var popId = "df-pop-" + uid;
    var pop = YB.el("div", {
      class: "date-pop", id: popId, role: "dialog", "aria-label": label, hidden: true
    });
    var head = YB.el("div", { class: "date-pop-head" });
    var prev = YB.el("button", {
      type: "button", class: "icon-btn date-nav", "data-date-nav": "-1",
      "aria-label": "上个月", html: svg("chevron-left")
    });
    var next = YB.el("button", {
      type: "button", class: "icon-btn date-nav", "data-date-nav": "1",
      "aria-label": "下个月", html: svg("chevron-right")
    });
    head.appendChild(prev);
    head.appendChild(YB.el("span", { class: "date-month", "aria-live": "polite" }));
    head.appendChild(next);
    pop.appendChild(head);
    // 星期表头必须逐字成项：.date-week 是 7 列 grid，单个文本节点会被当成一个
    // 匿名网格项塞进第一列（窄列里逐字折行，行高从 ~16px 涨到 68px）。
    var week = YB.el("div", { class: "date-week", "aria-hidden": "true" });
    for (var w = 0; w < WEEK.length; w++) week.appendChild(YB.el("span", { text: WEEK[w] }));
    pop.appendChild(week);
    pop.appendChild(YB.el("div", { class: "date-grid" }));
    var foot = YB.el("div", { class: "date-pop-foot" });
    foot.appendChild(YB.el("button", {
      type: "button", class: "btn btn--ghost btn--sm", "data-date-today": "1", text: "今天"
    }));
    pop.appendChild(foot);

    var trigger = YB.el("button", {
      type: "button", class: "date-trigger", "aria-haspopup": "dialog",
      "aria-expanded": "false", "aria-controls": popId
    });
    trigger.appendChild(YB.el("span", { class: "date-trigger-text" }));
    trigger.appendChild(YB.el("span", { class: "date-caret", "aria-hidden": "true", html: svg("calendar") }));

    root.insertBefore(trigger, input);
    root.appendChild(pop);
    labelFor(root, input);

    trigger.addEventListener("click", function () {
      if (pop.hidden) open(root); else close(root, true);
    });
    trigger.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); open(root); }
    });
    input.addEventListener("change", function () { paint(root); });
    bindPop(root, pop);
    paint(root);
  }

  function set(id, value) {
    var root = rootOf(id);
    var input = root ? inputOf(root) : null;
    if (!input) return;
    input.value = String(value == null ? "" : value);
    if (root) {
      var dt = parse(input.value);
      if (dt) setView(root, dt.getFullYear(), dt.getMonth() + 1);
      paint(root);
    }
  }

  function read(id) {
    var root = rootOf(id);
    var input = root ? inputOf(root) : null;
    return input ? input.value : "";
  }

  function setDisabled(id, on) {
    var root = rootOf(id);
    if (!root) return;
    var trigger = triggerOf(root), input = inputOf(root);
    if (trigger) trigger.disabled = !!on;
    if (input) input.disabled = !!on;
    root.classList.toggle("is-disabled", !!on);
    if (on) close(root, false);
  }

  function mount() {
    roots().forEach(build);
    if (globalBound) return;
    globalBound = true;
    document.addEventListener("click", function (e) {
      roots().forEach(function (root) {
        if (!root.contains(e.target)) close(root, false);
      });
    });
    // 视口变化后重新钳制：横竖屏切换/地址栏收起都会改变可用宽度，
    // 不重算的话弹层会停在旧位置上越界。只处理已打开的那些。
    window.addEventListener("resize", function () {
      roots().forEach(function (root) {
        if (root.classList.contains("is-open")) clampPop(root);
      });
    });
  }

  YB.dateField = { mount: mount, set: set, read: read, setDisabled: setDisabled };
})();
