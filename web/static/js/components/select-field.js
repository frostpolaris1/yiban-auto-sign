/* 模板化下拉选择（自研 listbox，取代原生 <select>）。

   挂载到 window.YB.selectField；classic script，公开面：
     mount()                初始化文档内所有 [data-select-field]（幂等）
     set(id, value)         写入选项值：同步隐藏 input、触发器文案与选中态
     read(id)               读取隐藏 input 的值
     setDisabled(id, on)    启用/禁用（整组置灰、触发键不可点）
     setOptions(id, items)  重建选项（动态列表用；items = [{v, t}, …]）

   为什么不用原生 <select>：下拉面板由 UA 渲染，高亮色 / 圆角 / 阴影 / 分隔线都不可控，
   与设计系统不一致（实测 Windows 下高亮为系统蓝、面板带系统投影）。自研 listbox 的面板
   完全用项目令牌着色，并能承载长列表的即时筛选。

   契约与 time-field 同口径：**隐藏 input 保留原 id 与值**，读取方（`$(id).value`）零改动；
   写入方必须走 set()（直接写隐藏 input 的 .value 不会更新可见文案）。
   用户选择时回写隐藏 input 并派发 change（程序化 set 不派发，避免误标脏）。

   可访问性：触发器 role=combobox + aria-expanded + aria-controls；面板 role=listbox、
   选项 role=option + aria-selected；↑/↓ 移动、Home/End 首末、Enter/Space 选择、
   Esc 收起并归还焦点、焦点移出整组时自动收起。选项静态写在模板里（Jinja 友好），
   也可由 setOptions() 注入；列表较长时面板顶部给筛选框。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var uid = 0;
  var globalBound = false;

  function svg(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  function roots() { return [].slice.call(document.querySelectorAll("[data-select-field]")); }
  function rootOf(id) { return document.querySelector('[data-select-field="' + id + '"]'); }
  function hiddenOf(root) { return root.querySelector("input[type=hidden]"); }
  function menuOf(root) { return root.querySelector(".select-menu"); }
  function triggerOf(root) { return root.querySelector(".select-trigger"); }
  function optionsOf(root) {
    return [].slice.call(root.querySelectorAll(".select-option"));
  }
  function visibleOptions(root) {
    return optionsOf(root).filter(function (o) { return !o.hidden; });
  }

  // 触发器要能被读屏关联到字段标签：优先用显式 aria-labelledby，否则就近取 .field-label
  function labelFor(root) {
    var trigger = triggerOf(root);
    if (!trigger) return;
    var explicit = root.getAttribute("aria-labelledby");
    if (explicit) { trigger.setAttribute("aria-labelledby", explicit); return; }
    var field = root.closest ? root.closest(".field") : null;
    var label = field ? field.querySelector(".field-label") : null;
    if (!label) return;
    if (!label.id) { uid += 1; label.id = "sf-label-" + uid; }
    trigger.setAttribute("aria-labelledby", label.id);
  }

  function optionByValue(root, v) {
    var hit = null;
    optionsOf(root).forEach(function (o) {
      if (hit === null && o.getAttribute("data-v") === v) hit = o;
    });
    return hit;
  }

  // 可见文案 = 选中项文本；无匹配值时退回首项（避免出现"空白控件"）
  function paint(root, v) {
    var input = hiddenOf(root);
    var hit = optionByValue(root, v);
    if (input && !hit) {
      var first = optionsOf(root)[0];
      if (first) { v = first.getAttribute("data-v"); input.value = v; hit = first; }
    }
    var text = triggerOf(root) && triggerOf(root).querySelector(".select-trigger-text");
    if (text) text.textContent = hit ? hit.textContent.trim() : "";
    optionsOf(root).forEach(function (o) {
      var on = o.getAttribute("data-v") === v;
      o.classList.toggle("is-sel", on);
      o.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  // 面板内聚焦选项一律 preventScroll：默认的"聚焦即滚动"会把整页拽一下
  // （实测点开下拉时窗口上跳），下拉自己的滚动条由 scrollOptionIntoView 单独负责。
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

  function close(root, back) {
    var menu = menuOf(root), trigger = triggerOf(root);
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    root.classList.remove("is-open");
    if (trigger) {
      trigger.setAttribute("aria-expanded", "false");
      if (back) trigger.focus();
    }
    var search = menu.querySelector(".select-search");
    if (search && search.value) { search.value = ""; applyFilter(root, ""); }
  }

  function focusSel(root) {
    var sel = optionByValue(root, (hiddenOf(root) || {}).value) || visibleOptions(root)[0];
    scrollOptionIntoView(root, sel);
    focusNoScroll(sel);
  }

  function open(root) {
    var menu = menuOf(root), trigger = triggerOf(root);
    if (!menu || !trigger || trigger.disabled) return;
    roots().forEach(function (r) { if (r !== root) close(r, false); });
    menu.hidden = false;
    root.classList.add("is-open");
    trigger.setAttribute("aria-expanded", "true");
    var search = menu.querySelector(".select-search");
    if (search) { focusNoScroll(search); if (search.select) search.select(); return; }
    focusSel(root);
  }

  function choose(root, opt) {
    if (opt.disabled) return;
    var input = hiddenOf(root);
    var v = opt.getAttribute("data-v");
    var changed = !!input && input.value !== v;
    if (input) input.value = v;
    paint(root, v);
    close(root, true);
    if (changed) input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function applyFilter(root, q) {
    var needle = String(q || "").trim().toLowerCase();
    var shown = 0;
    optionsOf(root).forEach(function (o) {
      var hit = !needle || o.textContent.toLowerCase().indexOf(needle) !== -1;
      o.hidden = !hit;
      if (hit) shown += 1;
    });
    var menu = menuOf(root);
    var empty = menu && menu.querySelector(".select-empty");
    if (empty) empty.hidden = shown > 0;
  }

  function searchBox(root) {
    var box = YB.el("input", {
      type: "text", class: "input select-search", autocomplete: "off",
      placeholder: "输入关键词筛选", "aria-label": "筛选选项"
    });
    box.addEventListener("input", function () { applyFilter(root, box.value); });
    return box;
  }

  function bindMenu(root, menu) {
    menu.addEventListener("click", function (e) {
      var opt = e.target.closest ? e.target.closest(".select-option") : null;
      if (opt) choose(root, opt);
    });
    menu.addEventListener("keydown", function (e) {
      var opts = visibleOptions(root);
      if (e.key === "Escape") { e.stopPropagation(); close(root, true); return; }
      if (e.key === "Enter" || e.key === " ") {
        if (e.target.classList && e.target.classList.contains("select-option")) {
          e.preventDefault();
          choose(root, e.target);
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
      if (menu && menu.hidden) open(root); else close(root, true);
    });
    trigger.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); open(root); }
    });
    root.addEventListener("focusout", function (e) {
      if (!root.contains(e.relatedTarget)) close(root, false);
    });
  }

  function set(id, value) {
    var root = rootOf(id);
    if (!root) return;
    var input = hiddenOf(root);
    if (!input) return;
    input.value = String(value == null ? "" : value);
    paint(root, input.value);
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
    if (on) close(root, false);
  }

  function optionNode(it) {
    return YB.el("button", {
      type: "button", class: "select-option", role: "option", "aria-selected": "false",
      "data-v": it.v == null ? "" : String(it.v), text: it.t == null ? "" : String(it.t)
    });
  }

  function setOptions(id, items) {
    var root = rootOf(id);
    var menu = root && menuOf(root);
    if (!menu) return;
    while (menu.firstChild) menu.removeChild(menu.firstChild);
    var list = items || [];
    if (root.hasAttribute("data-search") && list.length > 8) {
      menu.appendChild(searchBox(root));
    }
    list.forEach(function (it) { menu.appendChild(optionNode(it)); });
    if (root.hasAttribute("data-search") && list.length > 8) {
      menu.appendChild(YB.el("div", { class: "select-empty", hidden: true, text: "无匹配项" }));
    }
    paint(root, read(id));
  }

  function mount() {
    roots().forEach(function (root) {
      var id = root.getAttribute("data-select-field");
      var menu = menuOf(root);
      if (triggerOf(root)) { labelFor(root); return; }   // 已初始化
      var trigger = YB.el("button", {
        type: "button", class: "select-trigger", role: "combobox",
        "aria-haspopup": "listbox", "aria-expanded": "false"
      });
      trigger.appendChild(YB.el("span", { class: "select-trigger-text" }));
      trigger.appendChild(YB.el("span", {
        class: "select-caret", "aria-hidden": "true", html: svg("chevron-down")
      }));
      if (menu) {
        uid += 1;
        menu.id = "sf-list-" + uid;
        trigger.setAttribute("aria-controls", menu.id);
        root.insertBefore(trigger, menu);
        bindMenu(root, menu);
      } else {
        root.appendChild(trigger);
      }
      labelFor(root);
      bindRoot(root);
      set(id, read(id));
    });
    if (globalBound) return;
    globalBound = true;
    document.addEventListener("click", function (e) {
      roots().forEach(function (root) {
        if (!root.contains(e.target)) close(root, false);
      });
    });
  }

  YB.selectField = {
    mount: mount, set: set, read: read,
    setDisabled: setDisabled, setOptions: setOptions
  };
})();
