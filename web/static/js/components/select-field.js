/* 模板化下拉选择（自研 listbox，取代原生 <select>）。

   挂载到 window.YB.selectField；classic script，公开面：
     mount()                初始化文档内所有 [data-select-field]（幂等）
     set(id, value)         写入选项值：同步隐藏 input、触发器文案与选中态
     read(id)               读取隐藏 input 的值
     setDisabled(id, on)    启用/禁用（整组置灰、触发键不可点）
     setOptions(id, items)  重建选项（动态列表用），items 条目三形态：
                              { v, t }      可选选项
                              { group: 题 } 不可选小节头（原 optgroup label 语义）
                              { empty: 文 } 不可选空态行（如「（暂无）」）

   为什么不用原生 <select>：下拉面板由 UA 渲染，高亮色 / 圆角 / 阴影 / 分隔线都不可控，
   与设计系统不一致（实测 Windows 下高亮为系统蓝、面板带系统投影）。自研 listbox 的面板
   完全用项目令牌着色，并能承载长列表的即时筛选。

   契约与 time-field 同口径：**隐藏 input 保留原 id 与值**，读取方（`$(id).value`）零改动；
   写入方必须走 set()（直接写隐藏 input 的 .value 不会更新可见文案）。
   用户选择时回写隐藏 input 并派发 change（程序化 set 不派发，避免误标脏）。

   可访问性：触发器 role=combobox + aria-expanded + aria-controls；面板 role=listbox、
   选项 role=option + aria-selected；↑/↓ 移动、Home/End 首末、Enter/Space 选择、
   Esc 收起并归还焦点、焦点移出整组时自动收起。选项静态写在模板里（Jinja 友好），
   也可由 setOptions() 注入；列表较长时面板顶部给筛选框。

   弹层防裁剪（portal）：模态壳 .pm-panel 是 overflow:hidden、.modal-body 是
   overflow-y:auto，面板 absolute 定位必被裁掉；且 .pm-backdrop 的 backdrop-filter
   会创建层叠上下文。故打开时若 root 处于 .pm-panel 内，把面板 portal 到
   document.body 用 position:fixed 定位（z-index 1010 压过遮罩 1000、低于 Toast
   1200，与 row-menu 的 portal 先例同思路），宽度对齐触发器、上下空间不足时向上翻、
   一律钳制进视口；关闭时按记录的原父节点还原并清掉内联定位。页面场景（无模态壳）
   保持 absolute 原路径零变化。fixed 面板不随 .modal-body 滚动，故浮动期间监听
   scroll/resize：滚动源不是面板自身就立即关闭，避免面板与触发器脱节。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var uid = 0;
  var globalBound = false;
  var GAP = 8;                                 // 浮动面板与视口的安全边距
  var FLOAT_CLASS = "select-menu--floating";   // 浮动态（fixed + portal 到 body）
  var UP_CLASS = "is-up";                      // 向上展开（进入动画换方向）
  var floatGuard = null;                       // 浮动期间的 scroll/resize 关闭监听

  function svg(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  function roots() { return [].slice.call(document.querySelectorAll("[data-select-field]")); }
  function rootOf(id) { return document.querySelector('[data-select-field="' + id + '"]'); }
  function hiddenOf(root) { return root.querySelector("input[type=hidden]"); }
  /* 浮动期间面板不在 root 子树内，root.querySelector 查不到——portal 时把引用记到
     root.__floatMenu，还原时清掉。所有取面板/选项的路径都经这里，才与浮动兼容。 */
  function menuOf(root) {
    return root.__floatMenu || root.querySelector(".select-menu");
  }
  function triggerOf(root) { return root.querySelector(".select-trigger"); }
  function optionsOf(root) {
    var menu = menuOf(root);
    return menu ? [].slice.call(menu.querySelectorAll(".select-option")) : [];
  }
  function visibleOptions(root) {
    return optionsOf(root).filter(function (o) { return !o.hidden; });
  }
  // 命中判定要覆盖浮动面板：焦点/点击落在 portal 出去的面板上也算"落在整组内"
  function holdsFocus(root, node) {
    if (!node) return false;
    var menu = menuOf(root);
    return root.contains(node) || !!(menu && menu.contains(node));
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

  // 值 → 可见态的**纯解析**：命中返回对应项，未命中**原样返回该值**（渲染为未知态）。
  // 刻意不做"未知值退回首项"：那会静默把服务器上的未知枚举换成首项并回写隐藏 input，
  // 于是下一次无关保存把首项当成"用户改动"写进 .env（配置被静默改写）。
  function resolvePaintValue(opts, v) {
    var i;
    for (i = 0; i < opts.length; i++) {
      if (opts[i].v === v) return { value: v, hit: opts[i] };
    }
    return { value: v, hit: null };
  }

  // 可见文案 = 命中项文本；未命中时原样显示该值（未知态，不是空白、也不是首项）。
  // 不回写隐藏 input：值保持原样，保存侧按"未请求的键变化"防线拦截未知枚举。
  function paint(root, v) {
    var opts = optionsOf(root).map(function (o) {
      return { v: o.getAttribute("data-v"), node: o };
    });
    var r = resolvePaintValue(opts, v);
    var text = triggerOf(root) && triggerOf(root).querySelector(".select-trigger-text");
    if (text) text.textContent = r.hit ? r.hit.node.textContent.trim() : String(v == null ? "" : v);
    optionsOf(root).forEach(function (o) {
      var on = o.getAttribute("data-v") === r.value;
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
    // 浮动态收场：面板搬回 root 原位、清掉内联定位，供下次打开重新测量
    if (menu.classList.contains(FLOAT_CLASS)) {
      menu.classList.remove(FLOAT_CLASS, UP_CLASS);
      menu.style.left = menu.style.top = menu.style.width = "";
      if (menu.__home && menu.parentNode !== menu.__home) menu.__home.appendChild(menu);
      root.__floatMenu = null;
      disarmFloatGuard();
    }
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

  // 浮动定位：宽度对齐触发器（下拉语义等宽），水平夹进视口；垂直默认向下展开、
  // 下方放不下翻到上方，仍放不下（矮视口）就钳到能容纳的极限位置
  function placeFloat(root, menu) {
    var tr = triggerOf(root).getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;
    menu.style.width = Math.round(tr.width) + "px";
    var mw = menu.offsetWidth, mh = menu.offsetHeight;
    var left = tr.left;
    if (left + mw > vw - GAP) left = vw - GAP - mw;
    if (left < GAP) left = GAP;
    var top = tr.bottom + 4;                       // 与 absolute 态 top:calc(100% + 4px) 同距
    var up = top + mh > vh - GAP;
    if (up) top = tr.top - mh - 4;
    if (top + mh > vh - GAP) top = vh - GAP - mh;
    if (top < GAP) top = GAP;
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(top) + "px";
    menu.classList.toggle(UP_CLASS, up);
  }

  // fixed 面板不随 .modal-body 滚动：滚动/缩放的源不是面板自身就关闭，
  // 面板内部滚动（scroll target 在面板内）是正常交互，放行
  function armFloatGuard(root, menu) {
    disarmFloatGuard();
    floatGuard = function (e) {
      if (menu.contains(e.target)) return;
      close(root, false);
    };
    document.addEventListener("scroll", floatGuard, true);
    window.addEventListener("resize", floatGuard);
  }
  function disarmFloatGuard() {
    if (!floatGuard) return;
    document.removeEventListener("scroll", floatGuard, true);
    window.removeEventListener("resize", floatGuard);
    floatGuard = null;
  }

  function open(root) {
    var menu = menuOf(root), trigger = triggerOf(root);
    if (!menu || !trigger || trigger.disabled) return;
    roots().forEach(function (r) { if (r !== root) close(r, false); });
    menu.hidden = false;
    root.classList.add("is-open");
    trigger.setAttribute("aria-expanded", "true");
    // 模态壳内 absolute 面板必被 .pm-panel(overflow:hidden)/.modal-body(overflow-y:auto)
    // 裁剪 → portal 到 body 用 fixed；页面场景保持 absolute，行为零变化
    if (root.closest && root.closest(".pm-panel") && menu.parentNode !== document.body) {
      menu.__home = menu.parentNode;
      root.__floatMenu = menu;
      document.body.appendChild(menu);
      menu.classList.add(FLOAT_CLASS);
      placeFloat(root, menu);
      armFloatGuard(root, menu);
    }
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
      if (!holdsFocus(root, e.relatedTarget)) close(root, false);
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

  // 不可选小节头：不是 .select-option，天然不进键盘导航与 paint 选中态（原 optgroup label 语义）
  function groupNode(title) {
    return YB.el("div", { class: "select-group", role: "presentation", text: title });
  }

  // 不可选空态行（如「（暂无）」）：与筛选态的 .select-empty 同款样式，类名即语义
  function emptyNode(text) {
    return YB.el("div", { class: "select-empty", text: text });
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
    list.forEach(function (it) {
      if (!it) return;
      if (it.group != null) menu.appendChild(groupNode(String(it.group)));
      else if (it.empty != null) menu.appendChild(emptyNode(String(it.empty)));
      else menu.appendChild(optionNode(it));
    });
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
        if (!holdsFocus(root, e.target)) close(root, false);
      });
    });
  }

  YB.selectField = {
    mount: mount, set: set, read: read,
    setDisabled: setDisabled, setOptions: setOptions
  };
})();
