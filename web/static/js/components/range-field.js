/* 数值滑块字段（页面只显示数值，点击在弹窗里调整）。

   挂载到 window.YB.rangeField；classic script，公开面：
     mount()                初始化文档内所有 [data-range-field]（幂等）
     set(id, v)             写入数值：同步隐藏 input 与触发器文案
     read(id)               读取隐藏 input 的值
     setDisabled(id, on)    启用/禁用触发器

   为什么把滑块收进弹窗：滑杆只能表达"大概位置"，读不出精确值；直接铺在页面里时，
   一行"0–5 分钟"要吃掉整列宽度，而实际只需要一个数字（用户实测判"占位过大"）。
   弹窗里两种输入并存：数字框（可直接键入精确值）+ 滑杆（快速拖动），二者实时互同步，
   刻度按钮保留"常用值一点即达"。

   契约与 time-field 同口径：**隐藏 input 保留原 id 与值**，读取方零改动；
   写入方必须走 set()（写隐藏 input 的 .value 不会更新可见文案）。
   弹窗确认后才回写隐藏 input 并派发 change（取消不留痕，程序化 set 不派发）。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  function svg(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  function roots() { return [].slice.call(document.querySelectorAll("[data-range-field]")); }
  function rootOf(id) { return document.querySelector('[data-range-field="' + id + '"]'); }
  function hiddenOf(root) { return root.querySelector("input[type=hidden]"); }
  function triggerOf(root) { return root.querySelector(".range-trigger"); }

  function num(attr, fallback) {
    var v = parseFloat(attr);
    return isFinite(v) ? v : fallback;
  }
  function cfgOf(root) {
    var min = num(root.getAttribute("data-min"), 0);
    var max = num(root.getAttribute("data-max"), 1);
    var step = num(root.getAttribute("data-step"), 1);
    if (!(max > min) || !(step > 0)) step = 1;
    return {
      min: min, max: max, step: step,
      unit: root.getAttribute("data-unit") || "",
      title: root.getAttribute("data-title") || "数值",
      hint: root.getAttribute("data-hint") || ""
    };
  }
  // 浮点步进的显示与回写统一走这个：0.1 步长下 0.30000000000000004 会直接上屏
  function round(v) { return Math.round(v * 1000) / 1000; }
  function clamp(v, cfg) {
    v = round(Math.min(cfg.max, Math.max(cfg.min, v)));
    var steps = Math.round((v - cfg.min) / cfg.step);
    return round(Math.min(cfg.max, Math.max(cfg.min, cfg.min + steps * cfg.step)));
  }
  function fmt(v) { return String(round(v)); }

  function labelFor(root) {
    var trigger = triggerOf(root);
    if (!trigger) return;
    var explicit = root.getAttribute("aria-labelledby");
    if (explicit) { trigger.setAttribute("aria-labelledby", explicit); return; }
    var field = root.closest ? root.closest(".field") : null;
    var label = field ? field.querySelector(".field-label") : null;
    if (!label) return;
    if (!label.id) label.id = "rf-label-" + root.getAttribute("data-range-field");
    trigger.setAttribute("aria-labelledby", label.id);
  }

  function paint(root, v) {
    var cfg = cfgOf(root);
    var val = triggerOf(root) && triggerOf(root).querySelector(".range-trigger-val");
    if (val) val.textContent = fmt(v);
    var unit = triggerOf(root) && triggerOf(root).querySelector(".range-trigger-unit");
    if (unit) unit.textContent = cfg.unit;
  }

  function ticksFor(cfg) {
    var span = cfg.max - cfg.min;
    // 步长比 1 细、且量程够宽时，刻度取整数档（11 个 0.5 刻度读不出重点，6 个整数最好扫读）
    var step = (cfg.step < 1 && span >= 4) ? 1 : cfg.step;
    var out = [];
    for (var v = cfg.min; v <= cfg.max + 1e-9; v = round(v + step)) out.push(round(v));
    return out;
  }

  function open(root) {
    var id = root.getAttribute("data-range-field");
    var cfg = cfgOf(root);
    var trigger = triggerOf(root);
    if (!trigger || trigger.disabled) return;
    var current = clamp(num((hiddenOf(root) || {}).value, cfg.min), cfg);

    var number = YB.el("input", {
      class: "input range-modal-num", type: "number", inputmode: "decimal",
      min: fmt(cfg.min), max: fmt(cfg.max), step: fmt(cfg.step),
      "aria-label": cfg.title + "（" + cfg.unit + "）"
    });
    number.value = fmt(current);
    var group = YB.el("div", { class: "input-group range-modal-group" });
    group.appendChild(number);
    group.appendChild(YB.el("span", { class: "addon", text: cfg.unit }));
    // 右侧只放"量程与粒度"：当前值已由数字框与滑块位置两处表达，再回显一遍是重复信息
    var readout = YB.el("span", {
      class: "range-modal-tip",
      text: "范围 " + fmt(cfg.min) + "–" + fmt(cfg.max) + " " + cfg.unit + " · 步进 " + fmt(cfg.step)
    });
    var row = YB.el("div", { class: "range-modal-row" });
    row.appendChild(group);
    row.appendChild(readout);

    var range = YB.el("input", { class: "range", type: "range", "aria-label": cfg.title });
    range.min = fmt(cfg.min); range.max = fmt(cfg.max); range.step = fmt(cfg.step);
    range.value = fmt(current);

    var scale = YB.el("div", { class: "range-scale", role: "group", "aria-label": "常用刻度" });
    var tickNodes = ticksFor(cfg).map(function (v) {
      var b = YB.el("button", { type: "button", class: "range-tick", "data-v": fmt(v), text: fmt(v) });
      b.addEventListener("click", function () {
        range.value = fmt(v);
        number.value = fmt(v);
        sync();
        range.focus();
      });
      scale.appendChild(b);
      return b;
    });

    // 说明走弹窗副标题、不进正文：手机上软键盘把可视高度压到 ~400px 时，正文会变可滚动区，
    // 先被裁掉的是排在最后的东西——说明排在最前时，被裁掉的正是"输入 + 滑杆"这两个主控件
    // （实测 360×400 下滑杆只露 37%，压在底部按钮之下）。副标题常驻在头部，不会被裁。
    var body = YB.el("div", { class: "range-modal" });
    body.appendChild(row);
    body.appendChild(range);
    body.appendChild(scale);

    function sync() {
      var v = clamp(num(number.value, current), cfg);
      number.value = fmt(v);
      range.setAttribute("aria-valuetext", fmt(v) + " " + cfg.unit);
      tickNodes.forEach(function (b) {
        b.classList.toggle("is-active", b.getAttribute("data-v") === fmt(v));
      });
    }
    number.addEventListener("input", function () {
      var v = parseFloat(number.value);
      if (isFinite(v)) range.value = fmt(clamp(v, cfg));
      sync();
    });
    number.addEventListener("blur", function () {
      number.value = fmt(clamp(num(number.value, current), cfg));
      sync();
    });
    range.addEventListener("input", function () {
      number.value = fmt(clamp(num(range.value, current), cfg));
      sync();
    });
    sync();

    var stopReveal = null;
    YB.openModal({
      title: "调整" + cfg.title,
      subtitle: cfg.hint || "",
      body: body,
      onOpen: function () {
        number.focus(); if (number.select) number.select();
        // 软键盘把可视高度压到 ~400px 时，正文变可滚动区：把滑杆滚进可视，
        // 否则它被底部按钮压住一半（实测只露 37%）。聚焦与键盘弹出是两个时刻，故订阅视口变化。
        if (YB.keepRevealed) stopReveal = YB.keepRevealed(range);
      },
      onClose: function () { if (stopReveal) { stopReveal(); stopReveal = null; } },
      actions: [
        { label: "取消", variant: "ghost" },
        {
          label: "确定", variant: "primary",
          onClick: function () {
            var v = clamp(num(number.value, current), cfg);
            var input = hiddenOf(root);
            var changed = !!input && input.value !== fmt(v);
            if (input) input.value = fmt(v);
            paint(root, v);
            if (changed) input.dispatchEvent(new Event("change", { bubbles: true }));
          }
        }
      ]
    });
  }

  function set(id, value) {
    var root = rootOf(id);
    if (!root) return;
    var cfg = cfgOf(root);
    var v = clamp(num(value, cfg.min), cfg);
    var input = hiddenOf(root);
    if (input) input.value = fmt(v);
    paint(root, v);
  }

  function read(id) {
    var root = rootOf(id);
    var input = root ? hiddenOf(root) : null;
    return input ? input.value : "";
  }

  function setDisabled(id, on) {
    var root = rootOf(id);
    if (!root) return;
    var trigger = triggerOf(root);
    if (trigger) trigger.disabled = !!on;
    root.classList.toggle("is-disabled", !!on);
  }

  function mount() {
    roots().forEach(function (root) {
      var id = root.getAttribute("data-range-field");
      if (triggerOf(root)) { labelFor(root); return; }
      var cfg = cfgOf(root);
      var trigger = YB.el("button", {
        type: "button", class: "range-trigger", "aria-haspopup": "dialog",
        "aria-label": cfg.title
      });
      // 读屏名 = 标签（aria-labelledby），当前值走 aria-describedby 补报（见下）
      var val = YB.el("span", { class: "range-trigger-val", id: "rf-val-" + id });
      trigger.appendChild(val);
      trigger.appendChild(YB.el("span", { class: "range-trigger-unit" }));
      trigger.appendChild(YB.el("span", {
        class: "range-trigger-hint", "aria-hidden": "true", html: svg("pencil")
      }));
      trigger.lastChild.appendChild(document.createTextNode("调整"));
      trigger.setAttribute("aria-describedby", val.id);
      root.appendChild(trigger);
      labelFor(root);
      trigger.addEventListener("click", function () { open(root); });
      set(id, read(id));
    });
  }

  YB.rangeField = { mount: mount, set: set, read: read, setDisabled: setDisabled };
})();
