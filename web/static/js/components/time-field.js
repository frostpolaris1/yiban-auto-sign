/* 时间选择（滚动轮 · 弹窗内调整）。

   挂载到 window.YB.timeField；classic script，公开面：
     mount()          初始化文档内所有 [data-time-field] / [data-time-pair]（幂等）
     set(id, hhmm)    写入 "HH:MM"：同步隐藏 input 与触发器文案
     read(id)         读取隐藏 input 的 "HH:MM"

   为什么不用原生控件：input[type=time] 的选时列表由 UA 渲染，高亮色/黑框/圆角不可控
   （实测高亮 #0075FF 与项目主色 #2563EB 不同）；<select> 面板同理且选"分钟"要滚 60 项。
   为什么收进弹窗：两个「时:分」轮子组常驻页面要占 132px 高、且一行放不下起止两对，
   页面空间被控件吃掉；改成"页面只显示时间、点击在弹窗里滚轮调整"后，
   时间字段与其它控件同高（40px），弹窗里还能就地校验"开始早于结束"。

   契约：**隐藏 input 保留原 id 与 "HH:MM" 值**，读取方（`$(id).value`）零改动；
   写入方必须走 set()（写隐藏值不会更新可见文案）。弹窗确认后回写隐藏 input 并派发
   change（取消不留痕，程序化 set 不派发）。

   两种形态（标记即契约）：
     单个时间：<div class="time-field" data-time-field="sh-probe-time">…<input type=hidden id=…></div>
     起止区间：<div class="time-pair" data-time-pair> 内含两个 [data-time-field]（顺序 = 开始/结束）</div>
   区间共用一个触发器与一个弹窗 —— 起止是一组语义，拆成两个入口无法在提交前校验先后。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ITEM_H = 44;          // 与 .wheel-item 的行高一致（滚动定位按它换算）
  var uid = 0;
  var bound = false;

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function svg(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  function fieldOf(id) { return document.querySelector('[data-time-field="' + id + '"]'); }
  function hiddenOf(root) { return root.querySelector("input[type=hidden]"); }
  function triggerOf(root) { return root.querySelector(".time-trigger"); }
  function timeFields() { return [].slice.call(document.querySelectorAll("[data-time-field]")); }
  function pairs() { return [].slice.call(document.querySelectorAll("[data-time-pair]")); }
  function read(id) { var n = document.getElementById(id); return n ? n.value : ""; }
  function norm(v, fallback) {
    var s = String(v == null ? "" : v).trim().slice(0, 5);
    return /^\d{2}:\d{2}$/.test(s) ? s : fallback;
  }

  // 触发器要能被读屏关联到字段标签：优先用显式 aria-labelledby，否则就近取 .field-label
  function labelFor(host, trigger) {
    var explicit = host.getAttribute("aria-labelledby");
    if (explicit) { trigger.setAttribute("aria-labelledby", explicit); return; }
    var field = host.closest ? host.closest(".field") : null;
    var label = field ? field.querySelector(".field-label") : null;
    if (!label) return;
    if (!label.id) { uid += 1; label.id = "tf-label-" + uid; }
    trigger.setAttribute("aria-labelledby", label.id);
  }

  function buildTrigger(host, text, ariaLabel) {
    var trigger = YB.el("button", { type: "button", class: "time-trigger", "aria-haspopup": "dialog" });
    if (ariaLabel) trigger.setAttribute("aria-label", ariaLabel);
    trigger.appendChild(YB.el("span", { class: "time-trigger-val", text: text }));
    trigger.appendChild(YB.el("span", {
      class: "time-trigger-hint", "aria-hidden": "true", html: svg("clock")
    }));
    trigger.lastChild.appendChild(document.createTextNode("调整"));
    labelFor(host, trigger);
    return trigger;
  }

  /* ---------------- 滚动轮 ---------------- */
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
  function scrollToIndex(wheel, i) {
    wheel.scrollTop = i * ITEM_H;
    markSel(wheel, i);
  }
  function wheelValue(wheel) {
    var it = items(wheel)[indexOf(wheel)];
    return it ? it.getAttribute("data-v") : "00";
  }
  // 一组「时:分」：返回 {el, get(), set(hhmm)}
  function wheelGroup(initial) {
    var hw = buildWheel(24, "小时");
    var mw = buildWheel(60, "分钟");
    var box = YB.el("div", { class: "wheel-field" });
    box.appendChild(hw);
    box.appendChild(YB.el("span", { class: "wheel-sep", "aria-hidden": "true", text: ":" }));
    box.appendChild(mw);
    [hw, mw].forEach(function (w) {
      w.addEventListener("scroll", function () {
        markSel(w, indexOf(w));
      });
      w.addEventListener("keydown", function (e) {
        var i = indexOf(w), n = items(w).length, to = null;
        if (e.key === "ArrowUp") to = i - 1;
        else if (e.key === "ArrowDown") to = i + 1;
        else if (e.key === "PageUp") to = i - 5;
        else if (e.key === "PageDown") to = i + 5;
        else if (e.key === "Home") to = 0;
        else if (e.key === "End") to = n - 1;
        if (to == null) return;
        e.preventDefault();
        scrollToIndex(w, Math.max(0, Math.min(n - 1, to)));
      });
      w.addEventListener("click", function (e) {
        var it = e.target.closest ? e.target.closest(".wheel-item") : null;
        if (!it) return;
        scrollToIndex(w, Array.prototype.indexOf.call(items(w), it));
      });
    });
    return {
      el: box,
      set: function (hhmm) {
        var v = norm(hhmm, "00:00");
        scrollToIndex(hw, parseInt(v.slice(0, 2), 10));
        scrollToIndex(mw, parseInt(v.slice(3, 5), 10));
      },
      get: function () { return wheelValue(hw) + ":" + wheelValue(mw); }
    };
  }

  /* ---------------- 弹窗 ---------------- */
  // plan: { title, hint, groups: [{label, value}], onConfirm(values) → null 关闭 / 字符串 就地报错 }
  function openDialog(plan) {
    var body = YB.el("div", { class: "time-modal" });
    if (plan.hint) body.appendChild(YB.el("p", { class: "field-help", text: plan.hint }));
    var wrap = YB.el("div", { class: "time-groups" });
    var groups = plan.groups.map(function (g) {
      var box = YB.el("div", { class: "time-group" });
      if (g.label) box.appendChild(YB.el("span", { class: "time-group-label", text: g.label }));
      var wg = wheelGroup(g.value);
      box.appendChild(wg.el);
      wrap.appendChild(box);
      return wg;
    });
    body.appendChild(wrap);
    var err = YB.el("p", { class: "field-error", hidden: true });
    body.appendChild(err);
    YB.openModal({
      title: plan.title,
      body: body,
      onOpen: function () {
        plan.groups.forEach(function (g, i) { groups[i].set(g.value); });
      },
      actions: [
        { label: "取消", variant: "ghost" },
        {
          label: "确定", variant: "primary",
          onClick: function () {
            var values = groups.map(function (g) { return g.get(); });
            var bad = plan.onConfirm(values);
            if (bad) { err.textContent = bad; err.hidden = false; return false; }  // 不关闭，就地纠错
            return true;
          }
        }
      ]
    });
  }

  function apply(id, value) {
    var root = fieldOf(id);
    if (!root) return false;
    var input = hiddenOf(root);
    if (!input) return false;
    var v = norm(value, "");
    if (!v) return false;
    var changed = input.value !== v;
    input.value = v;
    paintTrigger(root, v);
    return changed;
  }

  function paintTrigger(root, v) {
    var trigger = triggerOf(root);
    if (trigger) {
      var slot = trigger.querySelector(".time-trigger-val");
      if (slot) slot.textContent = v;
    }
  }
  function paintPair(pair, startV, endV) {
    var trigger = triggerOf(pair);
    if (trigger) {
      var slot = trigger.querySelector(".time-trigger-val");
      if (slot) slot.textContent = startV + " 至 " + endV;
    }
  }

  function commitPair(pair, startV, endV) {
    var changed = apply(pair.getAttribute("data-start-id"), startV);
    var changedEnd = apply(pair.getAttribute("data-end-id"), endV);
    paintPair(pair, startV, endV);
    if (changed) document.getElementById(pair.getAttribute("data-start-id")).dispatchEvent(new Event("change", { bubbles: true }));
    if (changedEnd) document.getElementById(pair.getAttribute("data-end-id")).dispatchEvent(new Event("change", { bubbles: true }));
  }

  function openSingle(root) {
    var id = root.getAttribute("data-time-field");
    var title = (root.getAttribute("data-title") || "时间").trim();
    openDialog({
      title: "调整" + title,
      hint: root.getAttribute("data-hint") || "",
      groups: [{ label: "", value: norm(read(id), "00:00") }],
      onConfirm: function (values) {
        var changed = apply(id, values[0]);
        if (changed) document.getElementById(id).dispatchEvent(new Event("change", { bubbles: true }));
        return null;
      }
    });
  }

  function openPair(pair) {
    var startId = pair.getAttribute("data-start-id");
    var endId = pair.getAttribute("data-end-id");
    var startV = norm(read(startId), "06:30");
    var endV = norm(read(endId), "07:50");
    openDialog({
      title: pair.getAttribute("data-title") || "调整时间区间",
      hint: pair.getAttribute("data-hint") || "",
      groups: [
        { label: pair.getAttribute("data-start-label") || "开始", value: startV },
        { label: pair.getAttribute("data-end-label") || "结束", value: endV }
      ],
      onConfirm: function (values) {
        // 先后关系在提交点校验：这里就是把两个轮子绑成一个入口的理由
        if (values[0] >= values[1]) return "开始时间必须早于结束时间，请调整后再确认";
        commitPair(pair, values[0], values[1]);
        return null;
      }
    });
  }

  /* ---------------- 挂载 ---------------- */
  function setUpPair(pair) {
    if (triggerOf(pair)) return;
    var kids = [].slice.call(pair.querySelectorAll("[data-time-field]"));
    if (kids.length < 2) return;
    pair.setAttribute("data-start-id", kids[0].getAttribute("data-time-field"));
    pair.setAttribute("data-end-id", kids[1].getAttribute("data-time-field"));
    var startV = norm(read(pair.getAttribute("data-start-id")), "06:30");
    var endV = norm(read(pair.getAttribute("data-end-id")), "07:50");
    var trigger = buildTrigger(pair, startV + " 至 " + endV, pair.getAttribute("data-title") || "时间区间");
    trigger.addEventListener("click", function () { openPair(pair); });
    pair.insertBefore(trigger, pair.firstChild);
    kids.forEach(function (k) { k.hidden = true; });   // 隐藏 input 之外无可见内容
  }

  function setUpSingle(root) {
    if (triggerOf(root)) return;
    var id = root.getAttribute("data-time-field");
    var v = norm(read(id), "00:00");
    var trigger = buildTrigger(root, v, root.getAttribute("data-title") || "时间");
    trigger.addEventListener("click", function () { openSingle(root); });
    root.appendChild(trigger);
  }

  function mount() {
    pairs().forEach(setUpPair);
    timeFields().forEach(function (root) {
      if (root.closest("[data-time-pair]")) return;    // 归区间触发器统一管
      setUpSingle(root);
    });
    if (bound) return;
    bound = true;
  }

  // 程序化写入：只更新隐藏值与可见文案，不派发 change（避免把回填当成用户改动）
  function set(id, hhmm) {
    var v = norm(hhmm, "");
    if (!v) return;
    var root = fieldOf(id);
    if (root) paintTrigger(root, v);
    var pair = root && root.closest ? root.closest("[data-time-pair]") : null;
    var input = root ? hiddenOf(root) : null;
    if (input) input.value = v;
    if (pair) {
      paintPair(pair, norm(read(pair.getAttribute("data-start-id")), "00:00"),
        norm(read(pair.getAttribute("data-end-id")), "00:00"));
    }
  }

  YB.timeField = { mount: mount, set: set, read: read };
})();
