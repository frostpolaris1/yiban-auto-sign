/* 用户管理页（管理端 /users）行为。
   依赖 core.js 与 components/{row-menu,user-ops}.js。

   职责：拉取用户列表 + 已注销列表 → 渲染四组表格 → 处理搜索、组内选择/全选、
   折叠、行操作菜单与批量条；全部写操作委托 YB.userOps。

   安全（本页硬约束）：
   1. /api/users 返回**完整邮箱**，本页只在 state 内按内部 uid 持有；渲染一律用
      YB.maskEmail()，不把完整邮箱写入任何 DOM 文本或属性（不落 data-* 自定义属性、不落 title）。
      写操作从 uid 回查 state 取完整邮箱，只出现在请求体或既有契约的 URL path 中。
   2. 全页零 innerHTML 拼接后端数据：一律 YB.el / textContent / createTextNode。
   3. 不做轮询：用户列表变化低频（注册 / 注销 / 权限变更），旧页也不轮询；刷新只由
      写操作成功后就地重拉（GET /api/users + GET /api/users/deleted）触发。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;

  var state = {
    users: [], deleted: [], builtin: "admin",
    byUid: {}, uidOf: {}, uidSeq: 0,
    sel: { pending: {}, normal: {}, vacant: {}, deleted: {} },
    search: { pending: "", normal: "", vacant: "" },
    isMaster: false
  };
  var GROUPS = {
    pending: { tbody: "usr-pending-tbody", empty: "usr-pending-empty", count: "usr-pending-count",
               all: "usr-select-all-pending", bar: "usr-batch-pending", cnt: "usr-batch-count-pending",
               emptyText: "暂无待处理用户" },
    normal: { tbody: "usr-normal-tbody", empty: "usr-normal-empty", count: "usr-normal-count",
              all: "usr-select-all-normal", bar: "usr-batch-normal", cnt: "usr-batch-count-normal",
              emptyText: "暂无正式用户" },
    vacant: { tbody: "usr-vacant-tbody", empty: "usr-vacant-empty", count: "usr-vacant-count",
              all: "usr-select-all-vacant", bar: "usr-batch-vacant", cnt: "usr-batch-count-vacant",
              emptyText: "暂无空用户" },
    deleted: { tbody: "usr-deleted-tbody", empty: "usr-deleted-empty", count: "usr-deleted-count",
               all: "usr-select-all-deleted", bar: "usr-batch-deleted", cnt: "usr-batch-count-deleted",
               emptyText: "暂无已注销用户" }
  };

  /* ---------------- 小工具 ---------------- */
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function setText(id, v) { var n = $(id); if (n) n.textContent = v; }
  function debounce(fn, ms) {
    var timer = null;
    return function () { clearTimeout(timer); timer = setTimeout(fn, ms); };
  }
  // 内部 uid：仅在本页内存态映射到完整邮箱，不进 DOM。active 与已注销各用命名空间前缀，
  // 避免用户注销后重新注册时两条记录撞同一 uid。
  function uidFor(email, kind) {
    var k = (kind === "deleted" ? "d:" : "u:") + String(email).toLowerCase();
    if (!(k in state.uidOf)) state.uidOf[k] = "u" + (++state.uidSeq);
    return state.uidOf[k];
  }
  // 邮箱匹配：完整值与脱敏值都参与（列表只渲染脱敏串，输入完整邮箱也能搜到）。
  function matchKw(text, kw) {
    if (!kw) return true;
    var q = String(kw).toLowerCase();
    var s = String(text == null ? "" : text).toLowerCase();
    return s.indexOf(q) !== -1 || YB.maskEmail(text).toLowerCase().indexOf(q) !== -1;
  }
  function match(u, kw) { return matchKw(u.email, kw); }
  function listOf(group) {
    if (group === "deleted") return state.deleted.slice();
    return state.users.filter(function (u) {
      var review = u.review_count > 0;
      if (group === "pending") return review;
      if (group === "vacant") return !review && u.account_count === 0;
      return !review && u.account_count > 0;   // normal
    });
  }
  function filteredOf(group) {
    var kw = state.search[group] || "";
    return listOf(group).filter(function (u) { return match(u, kw); });
  }
  function selUids(group) {
    return Object.keys(state.sel[group]).filter(function (k) { return !!state.byUid[k]; });
  }

  /* ---------------- 数据加载 ---------------- */
  function pruneSelection() {
    Object.keys(state.sel).forEach(function (g) {
      Object.keys(state.sel[g]).forEach(function (k) { if (!state.byUid[k]) delete state.sel[g][k]; });
    });
  }
  function fetchUsers() {
    return YB.api("GET", "/api/users").then(function (data) {
      state.builtin = (data && data.builtin_admin) || "admin";
      state.users = ((data && data.users) || []).map(function (u) {
        var rec = {
          email: u.email || "", role: u.role || "user", created_at: u.created_at || "",
          account_count: Number(u.account_count) || 0, review_count: Number(u.review_count) || 0
        };
        rec.uid = uidFor(rec.email, "user");
        state.byUid[rec.uid] = rec;
        return rec;
      });
    });
  }
  function fetchDeleted() {
    return YB.api("GET", "/api/users/deleted").then(function (data) {
      state.deleted = ((data && data.items) || []).map(function (u) {
        var rec = {
          email: u.email || "", deleted_at: u.deleted_at || "",
          remaining_days: Number(u.remaining_days) || 0, status: u.status || "cooling"
        };
        rec.uid = uidFor(rec.email, "deleted");
        state.byUid[rec.uid] = rec;
        return rec;
      });
    });
  }
  function reload() {
    return Promise.all([fetchUsers(), fetchDeleted()]).then(function () {
      pruneSelection();
      renderAll();
    });
  }

  /* ---------------- 页面级加载状态（加载中 / 加载失败 + 重试） ---------------- */
  function setStatus(tone, text, retry) {
    var box = $("usr-status");
    if (!box) return;
    box.classList.remove("info", "danger");
    box.classList.add(tone);
    setText("usr-status-text", text);
    var btn = box.querySelector("[data-usr-retry]");
    if (btn) btn.hidden = !retry;
    box.hidden = false;
  }
  function hideStatus() { var box = $("usr-status"); if (box) box.hidden = true; }
  // 接口失败时四张表都可能空白：显式标「加载失败」，避免被误读成「没有用户」。
  function markLoadFailure() {
    Object.keys(GROUPS).forEach(function (g) {
      var empty = $(GROUPS[g].empty);
      if (!empty) return;
      var msg = empty.querySelector(".empty__msg");
      if (msg) msg.textContent = "加载失败";
      empty.hidden = false;
    });
  }
  // 首屏与重试共用；写操作后的 ctx.refresh() 走 reload()，不闪状态条。
  function startLoad() {
    setStatus("info", "正在加载用户列表…", false);
    return reload().then(hideStatus, function (e) {
      setStatus("danger", "用户列表加载失败", true);
      markLoadFailure();
      YB.toast.error((e && e.message) || "加载用户列表失败，请稍后重试");
    });
  }

  /* ---------------- 渲染 ---------------- */
  function badge(text, tone) { return YB.el("span", { class: "badge badge--" + tone, text: text }); }
  function roleBadge(role) {
    return role === "admin" ? badge("管理员", "info") : badge("普通用户", "muted");
  }
  function countCell(n) { return YB.el("td", { class: "usr-cell-count usr-col-md", text: String(n) }); }
  function timeCell(v) { return YB.el("td", { class: "usr-cell-time usr-col-md", text: v || "—" }); }

  // 窄屏（≤900）「待处理数 / 账号数 / 时间」三列被隐藏且本页无第二入口：在邮箱名称下
  // 补一行补充信息（始终渲染、由 CSS 断点控制 display，JS 不感知断点；同 account-table.js
  // 的 .acct-owner-inline 先例）。有内容才插入节点（无则不占位）；时间列改挂 title
  // （只放时间字符串，绝不含邮箱）。
  function mailCell(u, group) {
    var cell = YB.el("td", { class: "usr-cell-mail" });
    cell.appendChild(document.createTextNode(YB.maskEmail(u.email)));
    var parts = [];
    if (group === "pending" && u.review_count > 0) parts.push("待处理 " + u.review_count);
    if ((group === "normal" || group === "vacant") && u.account_count > 0) parts.push("账号 " + u.account_count);
    if (parts.length) cell.appendChild(YB.el("span", { class: "usr-inline-meta", text: parts.join(" · ") }));
    var time = u.created_at || u.deleted_at;
    if (time) cell.title = time;
    return cell;
  }

  function checkCell(u, group) {
    var wrap = YB.el("label", { class: "usr-check" });
    var cb = YB.el("input", { type: "checkbox", "aria-label": "选择用户 " + YB.maskEmail(u.email) });
    cb.checked = !!state.sel[group][u.uid];
    cb.addEventListener("change", function () {
      if (cb.checked) state.sel[group][u.uid] = true;
      else delete state.sel[group][u.uid];
      updateBatch(group);
      syncSelectAll(group, filteredOf(group));
    });
    wrap.appendChild(cb);
    return YB.el("td", { class: "usr-cell-check" }, [wrap]);
  }

  // 行操作菜单：动作回调闭包持有内部 uid，菜单/触发器不携带完整邮箱。
  // 目标为注册管理员且非主管理员时，后端对 role/重置密码/清空账号/删除用户统一 403
  // （app.py 6331-6337、6395-6396），故这些动作一律不给出。UI 隐藏不是安全边界：
  // 请求仍带 confirm_password，后端照旧复核。
  function menuItems(u, group) {
    var uid = u.uid;
    var items = [];
    var masterOnly = u.role === "admin" && !state.isMaster;
    if (state.isMaster && group === "normal") {
      var isAdmin = u.role === "admin";
      items.push({
        label: isAdmin ? "取消管理员" : "设为管理员", icon: "shield",
        run: function () { ops.role(uid, isAdmin ? "user" : "admin"); }
      });
    }
    if (masterOnly) return items;
    items.push({
      label: "重置密码", icon: "key",
      run: function () { askNewPassword(function (pw) { ops.resetPassword(uid, pw); }); }
    });
    if (group === "pending" || group === "normal") {
      items.push({
        label: "清空账号", icon: "circle-slash",
        run: function () { ops.deleteUser(uid, "accounts_only"); }
      });
    }
    items.push({
      label: "删除用户", icon: "trash", danger: true,
      run: function () { ops.deleteUser(uid, "full"); }
    });
    return items;
  }
  function menuCell(items, u) {
    if (!items.length) {
      // 非主管理员不可操作的注册管理员目标：不渲染空下拉，给出说明性文字
      return YB.el("td", { class: "usr-cell-actions" },
        [YB.el("span", { class: "usr-muted", text: "仅主管理员可操作" })]);
    }
    return YB.rowMenu.cell({
      items: items, cellClass: "usr-cell-actions",
      wrapClass: "dd-wrap usr-row-menu",
      // 逐行 aria-label 用脱敏邮箱区分（读屏不再全是「更多操作」）；不落完整邮箱
      label: "更多操作 " + YB.maskEmail(u.email)
    });
  }

  function userRow(u, group) {
    var tr = YB.el("tr");
    tr.appendChild(checkCell(u, group));
    tr.appendChild(mailCell(u, group));
    tr.appendChild(YB.el("td", { class: "usr-cell-role" }, [roleBadge(u.role)]));
    if (group === "pending") tr.appendChild(countCell(u.review_count));
    else if (group === "normal") tr.appendChild(countCell(u.account_count));
    tr.appendChild(timeCell(u.created_at));
    tr.appendChild(menuCell(menuItems(u, group), u));
    return tr;
  }

  function builtinRow(name) {
    var tr = YB.el("tr", { class: "usr-row-master" });
    var mail = YB.el("td", { class: "usr-cell-mail" });
    mail.appendChild(document.createTextNode(YB.maskEmail(name)));
    mail.appendChild(YB.el("span", { class: "usr-muted", text: "（主管理员）" }));
    tr.appendChild(YB.el("td", { class: "usr-cell-check" }));
    tr.appendChild(mail);
    tr.appendChild(YB.el("td", { class: "usr-cell-role" }, [badge("管理员", "info")]));
    tr.appendChild(YB.el("td", { class: "usr-cell-count usr-col-md", text: "—" }));
    tr.appendChild(YB.el("td", { class: "usr-cell-time usr-col-md", text: "—" }));
    tr.appendChild(YB.el("td", { class: "usr-cell-actions" }, [YB.el("span", { class: "usr-muted", text: "不可改" })]));
    return tr;
  }

  function remainText(u) {
    if (u.status === "purge_pending") return "—";
    return u.remaining_days >= 1 ? "剩余 " + u.remaining_days + " 天" : "不足一天";
  }
  function deletedRow(u) {
    var tr = YB.el("tr");
    tr.appendChild(checkCell(u, "deleted"));
    tr.appendChild(mailCell(u, "deleted"));
    tr.appendChild(timeCell(u.deleted_at));
    tr.appendChild(YB.el("td", { class: "usr-cell-remain usr-col-md", text: remainText(u) }));
    tr.appendChild(YB.el("td", { class: "usr-cell-status" },
      [u.status === "purge_pending" ? badge("待清除", "bad") : badge("冷却中", "warn")]));
    if (state.isMaster) {
      tr.appendChild(menuCell([{
        label: "立即清除", icon: "trash", danger: true,
        run: function () { ops.purge(u.uid); }
      }], u));
    } else {
      tr.appendChild(YB.el("td", { class: "usr-cell-actions" },
        [YB.el("span", { class: "usr-muted", text: "仅主管理员可清除" })]));
    }
    return tr;
  }

  // 计数文案统一为「（M 人匹配 / 共 N 人）」/「（N 人）」：组名已表达「待处理」等分组语义。
  function countLabel(total, shown, kw) {
    if (!total) return "";
    return kw ? "（" + shown + " 人匹配 / 共 " + total + " 人）" : "（" + total + " 人）";
  }

  function renderGroup(group, list, withBuiltin) {
    var refs = GROUPS[group];
    var tbody = $(refs.tbody);
    clear(tbody);
    var kw = state.search[group] || "";
    var filtered = list.filter(function (u) { return match(u, kw); });
    // 主管理员行同样受搜索约束：写死显示会让「可见行数 == 计数」不成立（空态也会误判）。
    var showBuiltin = !!withBuiltin && matchKw(state.builtin, kw);
    if (showBuiltin) tbody.appendChild(builtinRow(state.builtin));
    filtered.forEach(function (u) { tbody.appendChild(userRow(u, group)); });
    var total = list.length + (withBuiltin ? 1 : 0);
    var shown = filtered.length + (showBuiltin ? 1 : 0);
    var empty = $(refs.empty);
    var msg = empty.querySelector(".empty__msg");
    if (msg) msg.textContent = shown ? "" : (list.length ? "无匹配结果" : refs.emptyText);
    empty.hidden = shown > 0;
    setText(refs.count, countLabel(total, shown, kw));
    syncSelectAll(group, filtered);
    updateBatch(group);
  }

  function renderDeleted() {
    var card = $("usr-deleted-card");
    var hasDeleted = state.deleted.length > 0;
    card.hidden = !hasDeleted;
    // 标签与卡同步显隐：整组无数据时连标签一起收掉（否则点进去是一片空）
    var tabBtn = $("usr-tab-deleted");
    if (tabBtn) {
      var wasActive = !tabBtn.hidden && tabBtn.classList.contains("is-active");
      tabBtn.hidden = !hasDeleted;
      if (!hasDeleted && wasActive && YB.switchTab) YB.switchTab("pending");
    }
    var tbody = $("usr-deleted-tbody");
    clear(tbody);
    state.deleted.forEach(function (u) { tbody.appendChild(deletedRow(u)); });
    setText("usr-deleted-count", hasDeleted ? "（" + state.deleted.length + " 人）" : "");
    var empty = $("usr-deleted-empty");
    empty.hidden = hasDeleted;
    var msg = empty.querySelector(".empty__msg");
    if (msg) msg.textContent = hasDeleted ? "" : "暂无已注销用户";
    // 非主管理员不提供批量清除入口（后端仍会二次校验，UI 只是不给出不可能成功的动作）
    var purgeBtn = document.querySelector('[data-usr-batch="deleted:purge"]');
    if (purgeBtn) purgeBtn.hidden = !state.isMaster;
    syncSelectAll("deleted", state.deleted);
    updateBatch("deleted");
  }

  function renderAll() {
    // 重渲染会把承载行菜单的 <tr> 移出文档，此时观察者已脱附、restoreMenu 不再触发，
    // portal 到 <body> 的浮层会残留 —— 渲染前先立即拆除（不播退出动画）。
    YB.rowMenu.closeAll();
    renderGroup("pending", listOf("pending"), false);
    renderGroup("normal", listOf("normal"), true);
    renderGroup("vacant", listOf("vacant"), false);
    renderDeleted();
  }

  function updateBatch(group) {
    var n = selUids(group).length;
    setText(GROUPS[group].cnt, n);
    var bar = $(GROUPS[group].bar);
    if (bar) bar.hidden = n === 0;
  }
  function syncSelectAll(group, filtered) {
    var box = $(GROUPS[group].all);
    if (!box) return;
    var all = filtered.length > 0 && filtered.every(function (u) { return state.sel[group][u.uid]; });
    box.checked = all;
    box.indeterminate = !all && filtered.some(function (u) { return state.sel[group][u.uid]; });
  }

  /* ---------------- 写操作（委托 user-ops） ---------------- */
  var ops = YB.userOps.create({
    busy: function () { /* 本页无轮询与并发重建，忙碌态不改变视图 */ },
    refresh: function () { return reload(); },
    resolve: function (uid) { return state.byUid[uid]; }
  });

  // 重置口令入口：把统一口径 PW_POLICY_HINT 交给共享密码模态（长度 + 类别判定在模态内完成），
  // 新密码只作为参数传给 userOps，不落 DOM。
  function askNewPassword(cb) {
    var hint = (YB && YB.PW_POLICY_HINT) || (typeof PW_POLICY_HINT === "string" ? PW_POLICY_HINT : "");
    YB.openPasswordModal("设置新密码（" + hint + "）。重置后该用户的旧会话立即失效。", cb);
  }

  function doBatch(group, action) {
    var uids = selUids(group);
    if (!uids.length) return;
    if (action === "reset_password") askNewPassword(function (pw) { ops.batchReset(uids, pw); });
    else if (action === "delete") ops.batchDelete(uids);
    else if (action === "purge") ops.batchPurge(uids);
  }

  /* ---------------- 事件绑定 ---------------- */
  function bindSearch() {
    [["usr-pending-search", "pending"], ["usr-normal-search", "normal"], ["usr-vacant-search", "vacant"]]
      .forEach(function (pair) {
        var input = $(pair[0]);
        if (!input) return;
        input.addEventListener("input", debounce(function () {
          state.search[pair[1]] = input.value.trim();
          renderAll();
        }, 150));
      });
  }

  function bindSelectAll() {
    Object.keys(GROUPS).forEach(function (group) {
      var box = $(GROUPS[group].all);
      if (!box) return;
      box.addEventListener("change", function () {
        var filtered = filteredOf(group);
        state.sel[group] = {};
        if (box.checked) filtered.forEach(function (u) { state.sel[group][u.uid] = true; });
        renderAll();
      });
    });
  }

  function bindBatch() {
    document.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var btn = t.closest("[data-usr-batch]");
      if (btn) {
        var parts = btn.getAttribute("data-usr-batch").split(":");
        doBatch(parts[0], parts[1]);
        return;
      }
      var clr = t.closest("[data-usr-batch-clear]");
      if (clr) { state.sel[clr.getAttribute("data-usr-batch-clear")] = {}; renderAll(); return; }
      if (t.closest("[data-usr-retry]")) startLoad();
    });
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    bindSearch();
    bindBatch();
    bindSelectAll();
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      state.isMaster = !!me.is_builtin_admin;
      startLoad();
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
