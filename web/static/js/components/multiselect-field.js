/* 多选下拉（自研 listbox 的多选形态，取代"一组各自独立的开关"）。

   挂载到 window.YB.multiselectField；classic script，公开面：
     mount()               初始化文档内所有 [data-multiselect-field]（幂等）
     set(id, on)           写入**单个选项**的选中态（同步隐藏 input、触发器文案、勾选记号）
     get(id)               读取单个选项的选中态（boolean）
     setDisabled(id, on)   启用/禁用整组（落触发键）——同组的选项 id 共用一个触发器
     mount() 需在 select-field.mount() 之后无先后要求，两者各管各的 data-* 根。

   为什么一组独立开关不够：并排的两个开关读起来是"两项各自的功能"，用户看不出
   它们同属"选哪几天签到"这一件事，且"都不开"没有一句话可说清。本控件把一组可多选
   收进一个触发器，**触发器文案直接写明当前选中项**（"周六 · 周日" / "周六" / "周日" /
   "都不签"），四种组合一眼可分，且不靠色相区分。

   皮肤：沿用 30.1 的 .select-field / .select-trigger / .select-menu / .select-option
   （与 select-field.js 同一套，不另起）；只有多选特有的部分另加——勾选记号 .ms-tick、
   面板 aria-multiselectable、命中区抬到 44px。

   契约与 select-field 同口径：**每个选项一个隐藏 input**，保留原字段 id、值为
   "1"/"0"；读取方 `$(id).value === "1"` 即可，零改动。写入方必须走 set()（直接写
   隐藏 input 的 .value 不会更新触发器文案与勾选记号）。用户勾选时回写隐藏 input 并
   派发 change（bubbles），程序化 set 不派发，避免把回填误当用户改动而误标脏。

   可访问性：触发器 role=combobox + aria-haspopup=listbox + aria-expanded +
   aria-controls；面板 role=listbox + **aria-multiselectable=true**、选项 role=option +
   aria-selected；↑/↓ 移动、Home/End 首末、**空格/回车只切换不收起**（多选要能连选）、
   Esc 收起并归还焦点、焦点移出整组时自动收起。选项静态写在模板里（Jinja 友好）。

   收起不跳位（ux-guidelines #19）：面板 absolute 定位（与 .select-menu 同款）故展开
   不占布局高度，下方的行不会被推走；触发器 min-height 固定、勾选记号未选时也占位，
   选中/未选切换不改任何行高。面板落在 .pm-panel 内会被 overflow:hidden 裁掉——本页
   在普通页面里，若将来搬进模态须照 select-field.js 的 portal 路径处理。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var uid = 0;
  var globalBound = false;
  var EMPTY_TEXT = "都不选";   // 空态也必须有话可说：留空会被读成"没加载出来"

  function svg(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  function roots() { return [].slice.call(document.querySelectorAll("[data-multiselect-field]")); }
  function rootByOption(id) {
    var input = document.getElementById(id);
    return input && input.closest ? input.closest("[data-multiselect-field]") : null;
  }
  function inputIn(root, id) {
    var el = document.getElementById(id);
    return el && root.contains(el) ? el : null;
  }
  function menuOf(root) { return root.querySelector(".select-menu"); }
  function triggerOf(root) { return root.querySelector(".select-trigger"); }
  function optionsOf(root) {
    var menu = menuOf(root);
    return menu ? [].slice.call(menu.querySelectorAll(".select-option")) : [];
  }
  function visibleOptions(root) {
    return optionsOf(root).filter(function (o) { return !o.hidden; });
  }
  function holdsFocus(root, node) {
    if (!node) return false;
    var menu = menuOf(root);
    return root.contains(node) || !!(menu && menu.contains(node));
  }
  function isOn(id) {
    var el = document.getElementById(id);
    return !!el && el.value === "1";
  }
  // 触发器要能被读屏关联到字段标签：优先显式 aria-labelledby（复选组多在 .set-row 里，
  // 没有 .field/.field-label 可就近取，必须由模板显式指）
  function labelFor(root) {
    var trigger = triggerOf(root);
    if (!trigger) return;
    var explicit = root.getAttribute("aria-labelledby");
    if (explicit) { trigger.setAttribute("aria-labelledby", explicit); return; }
    var row = root.closest ? root.closest(".set-row") : null;
    var label = row ? row.querySelector(".set-row-label") : null;
    if (!label) return;
    if (!label.id) { uid += 1; label.id = "ms-label-" + uid; }
    trigger.setAttribute("aria-labelledby", label.id);
  }

  function optionText(opt) {
    var t = opt.querySelector(".ms-option-text");
    return (t ? t.textContent : opt.textContent).trim();
  }

  // 纯解析：触发器文案 = 命中项文本按**选项顺序**拼接（不按勾选先后，否则勾两遍顺序
  // 会变、文案抖动）。空态用 EMPTY_TEXT：留空会被读成"没加载出来"，不是"没选"。
  function paint(root) {
    var picked = [];
    optionsOf(root).forEach(function (o) {
      var on = isOn(o.getAttribute("data-v"));
      o.classList.toggle("is-sel", on);
      o.setAttribute("aria-selected", on ? "true" : "false");
      if (on) picked.push(optionText(o));
    });
    var trigger = triggerOf(root);
    var text = trigger && trigger.querySelector(".select-trigger-text");
    if (!text) return;
    text.textContent = picked.length ? picked.join(" · ") : EMPTY_TEXT;
    text.classList.toggle("is-empty", picked.length === 0);
  }

  // 面板内聚焦选项一律 preventScroll：默认的"聚焦即滚动"会把整页拽一下（同 select-field）
  function focusNoScroll(node) {
    if (!node) return;
    try { node.focus({ preventScroll: true }); } catch (e) { node.focus(); }
  }
  function scrollOptionIntoView(root, opt) {
    var menu = menuOf(root);
    if (!menu || !opt) return;
    var top = opt.offsetTop, h = menu.clientHeight, ih = opt.offsetHeight;
    if (top < menu.scrollTop || top + ih > menu.scrollTop + h) {
      menu.scrollTop = top - (h - ih) / 2;
    }
  }

  // 退场动效：与进场同参数反向（140ms / ease-in-strong，见 app.css 的 .is-closing）。
  // 动效结束（或同长兜底定时器）才 hidden=true；reduce 下直接收，不留"隐形但可聚焦"的空窗。
  var EXIT_MS = 140;
  function motionReduced() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }
  function isClosing(menu) { return menu.classList.contains("is-closing"); }
  function cancelClose(menu) {
    if (menu.__exitT) { clearTimeout(menu.__exitT); menu.__exitT = null; }
    menu.classList.remove("is-closing");
  }
  function finishClose(menu) {
    if (menu.__exitT) { clearTimeout(menu.__exitT); menu.__exitT = null; }
    menu.classList.remove("is-closing");
    menu.hidden = true;
  }

  function close(root, back) {
    var menu = menuOf(root), trigger = triggerOf(root);
    if (!menu || menu.hidden || isClosing(menu)) return;
    root.classList.remove("is-open");
    if (trigger) {
      trigger.setAttribute("aria-expanded", "false");
      if (back) focusNoScroll(trigger);
    }
    if (motionReduced()) { finishClose(menu); return; }
    menu.classList.add("is-closing");
    menu.__exitT = setTimeout(function () { finishClose(menu); }, EXIT_MS);
  }

  // 打开时落在**首个已选项**上（没有则首项）：多选里"回看当前选了哪些"比"从头扫"更省事
  function focusCurrent(root) {
    var sel = visibleOptions(root).filter(function (o) {
      return o.getAttribute("aria-selected") === "true";
    })[0] || visibleOptions(root)[0];
    scrollOptionIntoView(root, sel);
    focusNoScroll(sel);
  }

  function open(root) {
    var menu = menuOf(root), trigger = triggerOf(root);
    if (!menu || !trigger || trigger.disabled) return;
    roots().forEach(function (r) { if (r !== root) close(r, false); });
    cancelClose(menu);                    // 若刚在退场，取消收起、原样重开
    menu.hidden = false;
    root.classList.add("is-open");
    trigger.setAttribute("aria-expanded", "true");
    focusCurrent(root);
  }

  // 切换**不收起**、不挪焦点：多选要能连选（收起会让"再点一个"变成两次开合）
  function toggle(root, opt) {
    if (opt.disabled) return;
    var id = opt.getAttribute("data-v");
    var input = inputIn(root, id);
    if (!input) return;
    var next = input.value !== "1";
    input.value = next ? "1" : "0";
    paint(root);
    input.dispatchEvent(new Event("change", { bubbles: true }));
    scrollOptionIntoView(root, opt);
    focusNoScroll(opt);
  }

  function bindMenu(root, menu) {
    menu.addEventListener("click", function (e) {
      var opt = e.target.closest ? e.target.closest(".select-option") : null;
      if (opt) toggle(root, opt);
    });
    menu.addEventListener("keydown", function (e) {
      var opts = visibleOptions(root);
      if (e.key === "Escape") { e.stopPropagation(); close(root, true); return; }
      if (e.key === "Enter" || e.key === " ") {
        if (e.target.classList && e.target.classList.contains("select-option")) {
          e.preventDefault();
          toggle(root, e.target);
        }
        return;
      }
      var to = null;
      var i = opts.indexOf(document.activeElement);
      if (e.key === "ArrowDown") to = i + 1;
      else if (e.key === "ArrowUp") to = i - 1;
      else if (e.key === "Home") to = 0;
      else if (e.key === "End") to = opts.length - 1;
      if (to == null) return;
      e.preventDefault();
      if (to < 0) to = 0;
      if (to > opts.length - 1) to = opts.length - 1;
      scrollOptionIntoView(root, opts[to]);
      focusNoScroll(opts[to]);
    });
  }

  function bindRoot(root) {
    var trigger = triggerOf(root), menu = menuOf(root);
    if (!trigger) return;
    trigger.addEventListener("click", function () {
      if (!menu || menu.hidden || isClosing(menu)) open(root); else close(root, true);
    });
    trigger.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); open(root); }
    });
    root.addEventListener("focusout", function (e) {
      if (!holdsFocus(root, e.relatedTarget)) close(root, false);
    });
  }

  // 勾选记号由 JS 注入（模板只留占位空 span）：与 .check 的 16px 方框同尺寸同令牌，
  // 但这里**不能用 <input type=checkbox>**——选项本体是 <button>，交互元素不能嵌套
  function tickNode() {
    return YB.el("span", { class: "ms-tick", "aria-hidden": "true", html: svg("check") });
  }

  function mount() {
    roots().forEach(function (root) {
      var menu = menuOf(root);
      if (triggerOf(root)) { labelFor(root); paint(root); return; }   // 已初始化
      var trigger = YB.el("button", {
        type: "button", class: "select-trigger", role: "combobox",
        "aria-haspopup": "listbox", "aria-expanded": "false"
      });
      trigger.appendChild(YB.el("span", { class: "select-trigger-text" }));
      trigger.appendChild(YB.el("span", {
        class: "select-caret", "aria-hidden": "true", html: svg("chevron-down")
      }));
      optionsOf(root).forEach(function (o) {
        if (o.querySelector(".ms-tick")) return;
        o.insertBefore(tickNode(), o.firstChild);
      });
      if (menu) {
        uid += 1;
        menu.id = "ms-list-" + uid;
        trigger.setAttribute("aria-controls", menu.id);
        root.insertBefore(trigger, menu);
        bindMenu(root, menu);
      } else {
        root.appendChild(trigger);
      }
      labelFor(root);
      bindRoot(root);
      paint(root);
    });
    if (globalBound) return;
    globalBound = true;
    document.addEventListener("click", function (e) {
      roots().forEach(function (root) {
        if (!holdsFocus(root, e.target)) close(root, false);
      });
    });
  }

  // 写单个选项。回填页面常用，故不派发 change（同 select-field.set 的口径）
  function set(id, on) {
    var root = rootByOption(id);
    if (!root) return;
    var input = inputIn(root, id);
    if (!input) return;
    input.value = on ? "1" : "0";
    paint(root);
  }

  function get(id) { return isOn(id); }

  // 禁用按整组落：同组选项共用一个触发器，禁用其中一项等于整组不可改（调用方按组传任一 id）
  function setDisabled(id, on) {
    var root = rootByOption(id);
    if (!root) return;
    var trigger = triggerOf(root);
    if (trigger) trigger.disabled = !!on;
    root.classList.toggle("is-disabled", !!on);
    if (on) close(root, false);
  }

  YB.multiselectField = {
    mount: mount, set: set, get: get, setDisabled: setDisabled
  };
})();
