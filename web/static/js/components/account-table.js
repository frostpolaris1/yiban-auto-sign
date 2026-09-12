/* 管理端账号表格的行渲染（待处理 / 正常 / 待删除三组共用）。
   挂载到 window.YB.accountTable；classic script。

   本模块只负责「把一条账号数据变成 <tr>」，所有网络动作与组级状态由
   pages/accounts.js 通过 handlers 回调注入，便于三组复用同一套行结构。
   手机号一律使用列表接口下发的脱敏值（138****8000），完整号只在编辑/手动签到
   时由页面按需取详情接口，绝不进入本模块。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  function svg(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }

  var STATE_ICON = {
    success: "circle-check", already: "circle-check", failed: "circle-x",
    retrying: "refresh-cw", no_task: "circle-minus",
    skipped_window: "ban", skipped_norange: "ban",
    paused: "circle-pause", user_cancelled: "circle-stop", pending: "clock"
  };
  var STATE_TEXT = {
    success: "签到成功", already: "已签到", failed: "签到失败", retrying: "重试中",
    no_task: "无需签到", skipped_window: "时段外跳过", skipped_norange: "窗口缺失",
    paused: "账号暂停", user_cancelled: "用户已取消", pending: "待签"
  };
  var STATE_TONE = {
    success: "ok", already: "ok", failed: "bad", retrying: "warn", no_task: "muted",
    skipped_window: "warn", skipped_norange: "warn", paused: "bad",
    user_cancelled: "muted", pending: "muted"
  };

  function badge(status) {
    if (status === "pending") return YB.el("span", { class: "badge warning", text: "待审核" });
    if (status === "rejected") return YB.el("span", { class: "badge danger", text: "已拒绝" });
    return YB.el("span", { class: "badge success", text: "正常" });
  }

  function td(children, cls) {
    var cell = YB.el("td", cls ? { class: cls } : null);
    [].concat(children || []).forEach(function (c) {
      if (c == null) return;
      cell.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
    });
    return cell;
  }
  function btn(label, cls, onClick, opts) {
    opts = opts || {};
    var b = YB.el("button", { type: "button", class: cls });
    if (opts.icon) b.innerHTML = svg(opts.icon);
    if (opts.ariaLabel) b.setAttribute("aria-label", opts.ariaLabel);
    if (opts.title) b.title = opts.title;
    if (label) b.appendChild(document.createTextNode(label));
    b.addEventListener("click", onClick);
    return b;
  }

  // 状态列：图标 + title（状态名 · 原因 · 耗时）。颜色由 acct-state--* 的语义令牌给出。
  function stateCell(phone, states, msgs, durs) {
    var code = (states && states[phone]) || "pending";
    var msg = (msgs && msgs[phone]) || "";
    var dur = durs && durs[phone];
    var base = STATE_TEXT[code] || "待签";
    // 原因仅在不同于状态名时拼接，避免「签到成功 · 签到成功」式重复；耗时存在时追加
    var title = base + (msg && msg !== base ? " · " + msg : "")
      + (dur != null ? " · 耗时 " + Number(dur).toFixed(1) + "s" : "");
    var ico = YB.el("span", { class: "acct-state acct-state--" + (STATE_TONE[code] || "muted") });
    ico.innerHTML = svg(STATE_ICON[code] || "clock");
    ico.title = title;
    ico.setAttribute("aria-label", title);
    return td([ico], "acct-cell-state");
  }

  function checkCell(selected, account, onToggle) {
    var wrap = YB.el("label", { class: "acct-check" });
    var cb = YB.el("input", { type: "checkbox", "aria-label": "选择账号 " + (account.display_name || "") });
    cb.checked = !!selected;
    cb.addEventListener("change", function () { onToggle(cb.checked); });
    wrap.appendChild(cb);
    return td([wrap], "acct-cell-check");
  }

  // 窄屏（≤900，与 .acct-col-md 同档）：行操作收纳为一个图标按钮 + 下拉菜单。
  function isNarrow() {
    return !!(window.matchMedia && window.matchMedia("(max-width: 900px)").matches);
  }

  // 菜单项描述：动作分发仍走同一套 handlers（YB.accountOps），宽窄两版不各写一份 handler。
  function menuSpec(group, account, handlers) {
    if (group === "pending") {
      return [
        { label: "通过", icon: "check", run: function () { handlers.approve(account); } },
        { label: "驳回", icon: "x", run: function () { handlers.reject(account); } },
        { label: "编辑", icon: "pencil", run: function () { handlers.edit(account); } },
        { label: "删除", icon: "trash", danger: true, run: function () { handlers.remove(account); } }
      ];
    }
    if (group === "deleted") {
      return [
        { label: "恢复", icon: "rotate-ccw", run: function () { handlers.restore(account); } },
        { label: "彻底删除", icon: "trash", danger: true, run: function () { handlers.purge(account); } }
      ];
    }
    return [
      { label: "上移", icon: "arrow-up", run: function () { handlers.move(account, -1); } },
      { label: "下移", icon: "arrow-down", run: function () { handlers.move(account, 1); } },
      { label: "手动签到", icon: "play", run: function () { handlers.signin(account); } },
      { label: "编辑", icon: "pencil", run: function () { handlers.edit(account); } },
      { label: "删除", icon: "trash", danger: true, run: function () { handlers.remove(account); } }
    ];
  }

  // 行菜单浮动态：打开时把 .dd-menu portal 到 document.body 并用 fixed 定位，彻底绕开
  // .table-scroll / .collapse-inner 的 overflow 裁剪，以及祖先 transform 对 fixed 包含块的污染。
  // core.js 不提供开关回调，故用限定在本 wrap 上的 MutationObserver 观察 class：
  // 出现 is-open 即浮起并定位，消失即按记录的原父节点还原。菜单项数各组不同（待处理 4、
  // 正常 5、待删除 2），每次打开都按实际 offsetWidth/offsetHeight 重新定位。
  var GAP = 8; // 菜单与视口/触发器的安全边距

  function watchRowMenu(wrap) {
    if (!window.MutationObserver) return;
    var menu = wrap.querySelector(".dd-menu");
    if (!menu) return;
    var home = menu.parentNode;   // 原父节点（= wrap），关闭时按此还原，不做全局重建

    function place(trigger) {
      var tr = trigger.getBoundingClientRect();
      var mw = menu.offsetWidth, mh = menu.offsetHeight;
      var vw = window.innerWidth, vh = window.innerHeight;
      // 水平右对齐触发按钮右缘，再夹进视口（左 ≥ GAP）
      var left = Math.min(tr.right - mw, vw - mw - GAP);
      if (left < GAP) left = GAP;
      // 垂直默认向下；越界则翻到上方，再夹进视口（上 ≥ GAP）
      var top = tr.bottom + GAP;
      var up = top + mh > vh - GAP;
      if (up) top = tr.top - mh - GAP;
      if (top < GAP) top = GAP;
      menu.style.left = left + "px";
      menu.style.top = top + "px";
      menu.style.right = "auto";
      menu.style.bottom = "auto";
      menu.style.transformOrigin = up ? "bottom right" : "top right";
    }

    function floatMenu(trigger) {
      if (!trigger) return;
      if (menu.parentNode !== document.body) document.body.appendChild(menu);
      menu.classList.add("acct-menu--floating");
      place(trigger); // 先定位再显示：fixed + visibility:hidden 下 offsetWidth/Height 已可测
      requestAnimationFrame(function () {
        if (!menu.classList.contains("acct-menu--floating")) return;
        menu.classList.add("is-shown");
        // 菜单已 portal 到 body，Tab 不会再自然进入；打开时把焦点移入首项，
        // 配合下面的方向键处理与原生 Enter/Space，键盘可完整操作。
        var items = menu.querySelectorAll(".dd-menu-item");
        if (items.length) items[0].focus();
      });
    }

    function restoreMenu() {
      if (!menu.classList.contains("acct-menu--floating")) return;
      menu.classList.remove("is-shown", "acct-menu--floating");
      menu.style.left = menu.style.top = menu.style.right = menu.style.bottom = "";
      menu.style.transformOrigin = "";
      if (home && menu.parentNode !== home) home.appendChild(menu);
    }

    // core.js 的箭头键导航按 `.dd-wrap.is-open` 查 `.dd-menu-item`，菜单 portal 后查不到，
    // 故在浮动态内补一份同样的键位处理，避免 a11y 回退；Esc 仍由 core.js 的 document 监听关闭。
    menu.addEventListener("keydown", function (e) {
      var items = menu.querySelectorAll(".dd-menu-item");
      if (!items.length) return;
      var i = Array.prototype.indexOf.call(items, document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1 + items.length) % items.length].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
      else if (e.key === "Home") { e.preventDefault(); items[0].focus(); }
      else if (e.key === "End") { e.preventDefault(); items[items.length - 1].focus(); }
    });

    var observer = new MutationObserver(function () {
      if (wrap.classList.contains("is-open")) {
        floatMenu(wrap.querySelector("[data-dropdown]"));
      } else {
        // 关闭路径（点菜单项 / Esc / 点外部都由 core.js 去掉 is-open）：按原父节点还原。
        restoreMenu();
      }
    });
    observer.observe(wrap, { attributes: true, attributeFilter: ["class"] });
  }

  function dropdownCell(group, account, handlers) {
    var wrap = YB.el("div", { class: "dd-wrap acct-row-menu" });
    var trigger = YB.el("button", {
      type: "button", class: "btn btn--ghost btn--icon",
      "data-dropdown": "", "aria-haspopup": "menu", "aria-label": "更多操作", title: "更多操作"
    });
    trigger.innerHTML = svg("ellipsis");
    wrap.appendChild(trigger);
    var menu = YB.el("div", { class: "dd-menu", role: "menu" });
    var first = true;
    menuSpec(group, account, handlers).forEach(function (it) {
      // 破坏性项前加分隔线（.dd-divider），与既有下拉的普通项/危险项惯例一致
      if (it.danger && !first) menu.appendChild(YB.el("div", { class: "dd-divider" }));
      first = false;
      var item = YB.el("button", {
        type: "button", role: "menuitem",
        class: "dd-menu-item" + (it.danger ? " danger" : "")
      });
      item.innerHTML = svg(it.icon);
      item.appendChild(YB.el("span", { text: it.label }));
      item.addEventListener("click", function () { it.run(); });
      menu.appendChild(item);
    });
    wrap.appendChild(menu);
    watchRowMenu(wrap);
    return td([wrap], "acct-cell-actions");
  }

  function actionsCell(group, account, handlers) {
    if (isNarrow()) return dropdownCell(group, account, handlers);
    var box = YB.el("div", { class: "acct-row-actions" });
    if (group === "pending") {
      box.appendChild(btn("通过", "btn btn--primary btn--sm", function () { handlers.approve(account); }));
      box.appendChild(btn("驳回", "btn btn--ghost btn--sm", function () { handlers.reject(account); }));
      box.appendChild(btn("", "btn btn--ghost btn--icon", function () { handlers.edit(account); },
        { icon: "pencil", ariaLabel: "编辑", title: "编辑" }));
      box.appendChild(btn("", "btn btn--ghost btn--icon btn--danger-ghost", function () { handlers.remove(account); },
        { icon: "trash", ariaLabel: "删除", title: "删除" }));
    } else if (group === "deleted") {
      box.appendChild(btn("恢复", "btn btn--ghost btn--sm", function () { handlers.restore(account); }));
      box.appendChild(btn("彻底删除", "btn btn--ghost btn--sm btn--danger-ghost", function () { handlers.purge(account); }));
    } else {
      box.appendChild(btn("", "btn btn--ghost btn--icon", function () { handlers.move(account, -1); },
        { icon: "arrow-up", ariaLabel: "上移", title: "上移" }));
      box.appendChild(btn("", "btn btn--ghost btn--icon", function () { handlers.move(account, 1); },
        { icon: "arrow-down", ariaLabel: "下移", title: "下移" }));
      box.appendChild(btn("签到", "btn btn--ghost btn--sm", function () { handlers.signin(account); }));
      box.appendChild(btn("", "btn btn--ghost btn--icon", function () { handlers.edit(account); },
        { icon: "pencil", ariaLabel: "编辑", title: "编辑" }));
      box.appendChild(btn("", "btn btn--ghost btn--icon btn--danger-ghost", function () { handlers.remove(account); },
        { icon: "trash", ariaLabel: "删除", title: "删除" }));
    }
    return td([box], "acct-cell-actions");
  }

  function prefText(account) {
    if (!account.time_pref) return "—";
    var edge = account.time_pref_edge;
    if (edge === "first") return "最早 " + account.time_pref;
    if (edge === "last") return "最后 " + account.time_pref;
    return account.time_pref;
  }

  function ownerText(account) {
    return account.owner_display || (account.owner === "admin" ? "管理员" : (account.owner || "—"));
  }

  function row(opts) {
    var a = opts.account;
    var group = opts.group;
    var handlers = opts.handlers;
    var tr = YB.el("tr");
    tr.appendChild(checkCell(opts.selected, a, function (on) { opts.onToggle(a, on); }));
    if (group === "pending") {
      tr.appendChild(td([badge(a.status)], "acct-cell-audit"));
      tr.appendChild(td([a.display_name], "acct-cell-name"));
      tr.appendChild(td([String(a.phone || "")], "acct-cell-phone"));
      tr.appendChild(td([ownerText(a)], "acct-cell-owner acct-col-md"));
      tr.appendChild(actionsCell(group, a, handlers));
    } else if (group === "deleted") {
      tr.appendChild(td([a.display_name, YB.el("span", { class: "badge danger", text: "待删除" })], "acct-cell-name"));
      tr.appendChild(td([String(a.phone || "")], "acct-cell-phone"));
      tr.appendChild(td([ownerText(a)], "acct-cell-owner acct-col-md"));
      tr.appendChild(td([String(a.deleted_at || "").replace("T", " ").slice(0, 16)], "acct-cell-time"));
      tr.appendChild(actionsCell(group, a, handlers));
    } else {
      tr.appendChild(stateCell(a.phone, opts.states, opts.stateMsgs, opts.stateDurs));
      tr.appendChild(td([String((a.index != null ? a.index : 0) + 1)], "acct-cell-idx"));
      tr.appendChild(td([a.display_name], "acct-cell-name"));
      tr.appendChild(td([String(a.phone || "")], "acct-cell-phone"));
      tr.appendChild(td([a.phone_model || "—"], "acct-cell-model acct-col-lg"));
      tr.appendChild(td([prefText(a)], "acct-cell-pref acct-col-xl"));
      tr.appendChild(td([ownerText(a)], "acct-cell-owner acct-col-md"));
      tr.appendChild(td([badge(a.status)], "acct-cell-audit"));
      tr.appendChild(actionsCell(group, a, handlers));
    }
    return tr;
  }

  YB.accountTable = { row: row };
})();
