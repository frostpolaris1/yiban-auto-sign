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

  function actionsCell(group, account, handlers) {
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
