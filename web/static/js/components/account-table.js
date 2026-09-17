/* 管理端账号表格的行渲染（待处理 / 正常 / 待删除三组共用）。
   挂载到 window.YB.accountTable；classic script。

   本模块只负责「把一条账号数据变成 <tr>」，所有网络动作与组级状态由
   pages/work_accounts.js 通过 handlers 回调注入，便于三组复用同一套行结构。
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

  // 状态徽标用全站达标的 .badge--* 档位（vendor .badge.success 等不达 AA）；
  // 档位与 my-accounts.js 的先例对齐：已拒绝/待删除走 bad，待审核走 warn。
  function badge(status) {
    if (status === "pending") return YB.el("span", { class: "badge badge--warn", text: "待审核" });
    if (status === "rejected") return YB.el("span", { class: "badge badge--bad", text: "已拒绝" });
    return YB.el("span", { class: "badge badge--ok", text: "正常" });
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
  // omit 用于窄屏把主任务（通过）提到行外后，从菜单里去掉重复项。
  function menuSpec(group, account, handlers, omit) {
    var items;
    if (group === "pending") {
      items = [
        { label: "通过", icon: "check", run: function () { handlers.approve(account); } },
        { label: "驳回", icon: "x", run: function () { handlers.reject(account); } },
        { label: "编辑", icon: "pencil", run: function () { handlers.edit(account); } },
        { label: "删除", icon: "trash", danger: true, run: function () { handlers.remove(account); } }
      ];
    } else if (group === "deleted") {
      items = [
        { label: "恢复", icon: "rotate-ccw", run: function () { handlers.restore(account); } },
        { label: "彻底删除", icon: "trash", danger: true, run: function () { handlers.purge(account); } }
      ];
    } else {
      items = [
        { label: "上移", icon: "arrow-up", run: function () { handlers.move(account, -1); } },
        { label: "下移", icon: "arrow-down", run: function () { handlers.move(account, 1); } },
        { label: "手动签到", icon: "play", run: function () { handlers.signin(account); } },
        { label: "编辑", icon: "pencil", run: function () { handlers.edit(account); } },
        { label: "删除", icon: "trash", danger: true, run: function () { handlers.remove(account); } }
      ];
    }
    if (omit && omit.length) {
      items = items.filter(function (it) { return omit.indexOf(it.label) === -1; });
    }
    return items;
  }

  // 行菜单的浮动态（portal 到 body + fixed 定位 + 视口钳制 + 还原）由共享组件
  // YB.rowMenu 提供；本模块只把「动作清单」交给它，宽窄两版共用同一份 handlers。
  function dropdownCell(group, account, handlers, omit) {
    return YB.rowMenu.cell({
      items: menuSpec(group, account, handlers, omit),
      cellClass: "acct-cell-actions",
      wrapClass: "dd-wrap acct-row-menu",
      label: "更多操作"
    });
  }

  function actionsCell(group, account, handlers) {
    if (isNarrow()) {
      // 窄屏审核主任务（通过）提到行外可见，气味与点击成本都不再依赖「更多操作」；
      // 其余动作仍收进菜单。YB.rowMenu.cell 返回 <td>，取其首子节点（.dd-wrap）复用到行内。
      if (group === "pending") {
        var box = YB.el("div", { class: "acct-row-actions acct-row-actions--narrow" });
        box.appendChild(btn("通过", "btn btn--primary btn--sm", function () { handlers.approve(account); }));
        var menuTd = dropdownCell(group, account, handlers, ["通过"]);
        box.appendChild(menuTd.firstChild);
        return td([box], "acct-cell-actions");
      }
      return dropdownCell(group, account, handlers);
    }
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

  // 「上次实领」= 上一个业务日实际领取该账号的执行体。取值只有一个来源：列表接口的
  // last_executor（{role, index, label}）——前端不解析身份串、不按 role 自行归类
  // （unknown 是存量数据的事实，后端照实回 label）。
  // null = 上一个业务日没有该账号的记录（含新账号、库未初始化）→ 显示「—」，不当成 unknown。
  function lastExecText(account) {
    var ex = account && account.last_executor;
    if (!ex) return "—";
    var label = String(ex.label || "");
    if (label) return label;
    // 契约保证 label 字段存在，此处仅兜底字段缺失：unknown 照实说「旧数据」，其余不猜。
    return ex.role === "unknown" ? "未标注（旧数据）" : "—";
  }

  // 三组共用同一格：列位置固定在「手机号」之后、「归属」之前（两个「归属」会打架，故列名不同）。
  // 断点档位与模板表头 th 上的类名必须一致（改档位要同时改模板的 th）。
  // 取 xl 档（≤1280 隐藏）：三张表在 ≤1280 已占满可用宽度（实测 1280 下正常账号表本就有
  // 101px 内滚），本列宽约 100~132px，放宽一档会在 1101~1280 造出新的表内横向滚动。
  function lastExecCell(account) {
    return td([lastExecText(account)], "acct-cell-lastexec acct-col-lg");
  }

  // 归属邮箱：列表态 a.owner 已由后端 _mask_email 脱敏为 use***@example.com，直接展示即可，
  // 不在前端还原/请求完整邮箱。非邮箱归属（admin/无）回落 ownerText。
  // 取值只在此一处，日后设置页加「是否显示归属邮箱」开关时只改这里或包一层布尔判断。
  function ownerMailText(account) {
    var owner = String((account && account.owner) || "");
    return owner.indexOf("@") !== -1 ? owner : ownerText(account);
  }

  // 名称单元格：名称（删除组带「待删除」徽章，与名称同行）+ 窄屏补充的归属邮箱小字。
  // 读屏顺序为「名称 → 归属邮箱」（邮箱节点在名称之后）。
  // 窄屏归属邮箱由浏览器级偏好（设置页「竖屏显示归属邮箱」）控制显隐：偏好读取集中在
  // YB.prefs.ownerEmailVisible()，此处与 ownerMailText() 是唯一的取用对；宽屏归属列不受影响。
  function nameCell(account, withDeletedBadge) {
    var main = YB.el("div", { class: "acct-name-main" });
    main.appendChild(document.createTextNode(String(account.display_name || "")));
    if (withDeletedBadge) main.appendChild(YB.el("span", { class: "badge badge--bad", text: "待删除" }));
    var cell = td([main], "acct-cell-name");
    if (!YB.prefs || YB.prefs.ownerEmailVisible()) {
      cell.appendChild(YB.el("span", { class: "acct-owner-inline", text: ownerMailText(account) }));
    }
    return cell;
  }

  function row(opts) {
    var a = opts.account;
    var group = opts.group;
    var handlers = opts.handlers;
    var tr = YB.el("tr");
    // 供写操作成功后就地反馈（高亮/待签中）按索引找回重建后的行；index 非敏感
    if (a.index != null) tr.setAttribute("data-acct-idx", String(a.index));
    tr.appendChild(checkCell(opts.selected, a, function (on) { opts.onToggle(a, on); }));
    if (group === "pending") {
      tr.appendChild(td([badge(a.status)], "acct-cell-audit"));
      tr.appendChild(nameCell(a, false));
      tr.appendChild(td([String(a.phone || "")], "acct-cell-phone"));
      tr.appendChild(lastExecCell(a));
      tr.appendChild(td([ownerText(a)], "acct-cell-owner acct-col-md"));
      tr.appendChild(actionsCell(group, a, handlers));
    } else if (group === "deleted") {
      tr.appendChild(nameCell(a, true));
      tr.appendChild(td([String(a.phone || "")], "acct-cell-phone"));
      tr.appendChild(lastExecCell(a));
      tr.appendChild(td([ownerText(a)], "acct-cell-owner acct-col-md"));
      tr.appendChild(td([String(a.deleted_at || "").replace("T", " ").slice(0, 16)], "acct-cell-time"));
      tr.appendChild(actionsCell(group, a, handlers));
    } else {
      tr.appendChild(stateCell(a.phone, opts.states, opts.stateMsgs, opts.stateDurs));
      tr.appendChild(td([String((a.index != null ? a.index : 0) + 1)], "acct-cell-idx"));
      tr.appendChild(nameCell(a, false));
      tr.appendChild(td([String(a.phone || "")], "acct-cell-phone"));
      tr.appendChild(lastExecCell(a));
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
