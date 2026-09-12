/* 账号管理页（管理端 /accounts）行为。
   依赖 core.js（YB.api/el/toast/confirmDialog/identity）与共享组件
   （components/account-form.js 表单、account-table.js 行、account-ops.js 写操作）。

   职责：拉取并缓存账号列表 + 签到状态 → 渲染三组表格与统计/容量卡 → 处理搜索、
   组内全选、按需取完整手机号（编辑走表单组件，手动签到走 ops）、页面可见时静默轮询。
   写操作（含批量与口令二次鉴权）全部委托 account-ops，本文件只维护视图状态。

   脱敏：列表层手机号已脱敏；完整号只在 account-form/account-ops 的请求回调内流转，
   绝不写入常驻 DOM 文本或日志。防错位与单次上限见 account-ops。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;
  var ANIM_MIN_MS = 80;         // 短于此值的刷新不播进入动画

  var state = {
    accounts: [], states: {}, stateMsgs: {}, stateDurs: {},
    pendingSearch: "", activeSearch: "", deletedSearch: "",
    sel: { pending: {}, active: {}, deleted: {} },
    busy: false, lastSnap: ""
  };
  var GROUPS = {
    pending: { tbody: "accounts-pending-tbody", empty: "accounts-pending-empty", count: "accounts-pending-count", bar: "batch-bar-pending", cnt: "batch-count-pending", all: "select-all-pending" },
    active: { tbody: "accounts-tbody", empty: "accounts-empty", count: "accounts-active-count", bar: "batch-bar-active", cnt: "batch-count-active", all: "select-all-active" },
    deleted: { tbody: "accounts-deleted-tbody", empty: "accounts-deleted-empty", count: "accounts-deleted-count", bar: "batch-bar-deleted", cnt: "batch-count-deleted", all: "select-all-deleted" }
  };

  /* ---------------- 小工具 ---------------- */
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function setVal(id, value) {
    var node = $(id);
    if (!node) return;
    clear(node);
    node.appendChild(document.createTextNode(String(value)));
  }
  function debounce(fn, ms) {
    var timer = null;
    return function () {
      clearTimeout(timer);
      timer = setTimeout(fn, ms);
    };
  }
  // 列表已脱敏：输入完整号时同样 mask 后匹配，保证搜索可用
  function maskPhone(p) {
    p = String(p || "");
    if (p.indexOf("*") !== -1) return p;
    return p.length >= 7 ? p.slice(0, 3) + "****" + p.slice(-4) : p;
  }
  function accountMatch(a, kw) {
    if (!kw) return true;
    var q = String(kw).toLowerCase();
    var masked = maskPhone(q);
    return [a.name, a.phone, a.owner_display, a.owner].some(function (v) {
      var s = String(v || "").toLowerCase();
      return s.indexOf(q) !== -1 || s.indexOf(masked) !== -1;
    });
  }
  function byIndex(idx) {
    return state.accounts.filter(function (a) { return a.index === idx; })[0];
  }
  function selectedIds(group) {
    return Object.keys(state.sel[group]).map(Number).filter(function (n) { return !isNaN(n); });
  }
  function selectedPhones(group) {
    return selectedIds(group).map(function (i) { var a = byIndex(i); return a ? a.phone : ""; });
  }

  /* ---------------- 数据加载 ---------------- */
  function pruneSelection() {
    Object.keys(state.sel).forEach(function (g) {
      Object.keys(state.sel[g]).forEach(function (k) {
        if (!byIndex(Number(k))) delete state.sel[g][k];
      });
    });
  }

  function load(silent) {
    var t0 = performance.now();
    return YB.api("GET", "/api/accounts").then(function (data) {
      state.accounts = (data && data.accounts) || [];
      state.states = (data && data.states) || {};
      state.stateMsgs = (data && data.state_msgs) || {};
      state.stateDurs = (data && data.state_durs) || {};
      pruneSelection();
      // 指纹覆盖所有会进 DOM 的字段：只比 accounts/states 时，仅「状态原因/耗时」变化
      // 不会触发重渲染，表格 title 会停留在旧值。
      var snap = JSON.stringify([state.accounts, state.states, state.stateMsgs, state.stateDurs]);
      if (silent && snap === state.lastSnap) return;   // 无变化不重建 DOM
      state.lastSnap = snap;
      var slow = silent && (performance.now() - t0 > ANIM_MIN_MS);
      var root = $("accounts-root");
      if (slow && root) {
        // 先挂 is-swapping（透明度过渡起步），隔两帧再渲染并移除：中间留出至少一帧，
        // 160ms 的过渡才来得及被浏览器采样；同帧 add→render→remove 只会闪一下。
        root.classList.add("is-swapping");
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            renderAll();
            root.classList.remove("is-swapping");
          });
        });
      } else {
        renderAll();
      }
    }).catch(function (e) {
      if (!silent) YB.toast.error(e.message);
    });
  }

  /* ---------------- 渲染 ---------------- */
  function sortedPending() {
    // 待审核置顶（新提交在前）、已拒绝沉底；accounts 表无时间戳，id 与提交先后单调一致，
    // 以 index 作时间代理。显示顺序不影响批量操作（按 index + phones 对齐校验）。
    return state.accounts.filter(function (a) {
      return (a.status === "pending" || a.status === "rejected") && !a.deleted;
    }).sort(function (a, b) {
      if (a.status === b.status) return b.index - a.index;
      return a.status === "pending" ? -1 : 1;
    });
  }
  function groupAll(group) {
    if (group === "pending") return sortedPending();
    if (group === "active") {
      return state.accounts.filter(function (a) { return a.status === "active" && !a.deleted; });
    }
    return state.accounts.filter(function (a) { return a.deleted; });
  }
  function emptyDefault(group) {
    if (group === "pending") return "暂无待处理账号";
    if (group === "deleted") return "暂无待删除账号";
    return "暂无账号，点右上角「添加账号」配置";
  }

  function renderGroup(group, all, filtered) {
    var refs = GROUPS[group];
    var tbody = $(refs.tbody);
    clear(tbody);
    filtered.forEach(function (a) {
      tbody.appendChild(YB.accountTable.row({
        group: group, account: a, selected: !!state.sel[group][a.index],
        states: state.states, stateMsgs: state.stateMsgs, stateDurs: state.stateDurs,
        handlers: handlers,
        onToggle: function (acc, on) {
          if (on) state.sel[group][acc.index] = true;
          else delete state.sel[group][acc.index];
          updateBatchBar(group);
          syncSelectAll(group, filtered);
        }
      }));
    });
    var empty = $(refs.empty);
    var msg = empty.querySelector(".empty__msg");
    if (msg) {
      msg.textContent = filtered.length ? "" : (all.length ? "无匹配结果" : emptyDefault(group));
    }
    empty.hidden = filtered.length > 0;
    var kw = state[group + "Search"];
    var label = kw ? filtered.length + " 个匹配 / 共 " + all.length + " 个" : all.length + " 个";
    $(refs.count).textContent = all.length ? label : "";
    syncSelectAll(group, filtered);
    updateBatchBar(group);
  }

  function renderStats(active) {
    var success = 0, failed = 0, waiting = 0, skipped = 0;
    active.forEach(function (a) {
      var s = state.states[a.phone] || "pending";
      if (s === "success" || s === "already") success++;
      else if (s === "failed") failed++;
      else if (s === "no_task" || s === "skipped_window" || s === "skipped_norange" ||
               s === "paused" || s === "user_cancelled") skipped++;
      else waiting++;
    });
    setVal("stat-success", success);
    setVal("stat-failed", failed);
    setVal("stat-waiting", waiting);
    setVal("stat-skipped", skipped);
  }

  function renderAll() {
    var pending = sortedPending();
    var active = groupAll("active");
    var deleted = groupAll("deleted");
    renderGroup("pending", pending, pending.filter(function (a) { return accountMatch(a, state.pendingSearch); }));
    renderGroup("active", active, active.filter(function (a) { return accountMatch(a, state.activeSearch); }));
    renderGroup("deleted", deleted, deleted.filter(function (a) { return accountMatch(a, state.deletedSearch); }));
    renderStats(active);
    var tip = $("pending-tip");
    if (tip) tip.hidden = pending.length === 0;
    setVal("pending-count", pending.length);
  }

  function updateBatchBar(group) {
    var refs = GROUPS[group];
    var n = selectedIds(group).length;
    $(refs.cnt).textContent = n;
    $(refs.bar).hidden = n === 0;
  }

  function syncSelectAll(group, filtered) {
    var box = $(GROUPS[group].all);
    if (!box) return;
    var allSelected = filtered.length > 0 && filtered.every(function (a) { return state.sel[group][a.index]; });
    box.checked = allSelected;
    box.indeterminate = !allSelected && filtered.some(function (a) { return state.sel[group][a.index]; });
  }

  /* ---------------- 写操作（委托 account-ops） ---------------- */
  var ops = YB.accountOps.create({
    busy: function (on) { state.busy = on; },
    refresh: function () { load(); },
    onBatchSuccess: function () {
      Object.keys(state.sel).forEach(function (g) { state.sel[g] = {}; });
    }
  });

  var handlers = {
    approve: function (a) { ops.review(a, "approve"); },
    reject: function (a) { ops.review(a, "reject"); },
    edit: editAccount,
    remove: function (a) { ops.remove(a); },
    restore: function (a) { ops.restore(a); },
    purge: function (a) { ops.purge(a); },
    move: function (a, dir) { ops.move(a, dir); },
    signin: function (a) { ops.signin(a); }
  };

  function editAccount(a) {
    YB.accountForm.open({
      variant: "admin", index: a.index, account: a, detail: true, allowEmail: true,
      endpoints: { create: "/api/accounts", update: "/api/accounts/" },
      onSaved: function () { load(); }
    });
  }
  function addAccount() {
    YB.accountForm.open({
      variant: "admin", index: null, account: null, detail: false, allowEmail: true,
      endpoints: { create: "/api/accounts", update: "/api/accounts/" },
      onSaved: function () { load(); }
    });
  }

  /* ---------------- 事件绑定 ---------------- */
  function bindSearch() {
    [["pending-search", "pendingSearch"], ["active-search", "activeSearch"], ["deleted-search", "deletedSearch"]]
      .forEach(function (pair) {
        var input = $(pair[0]);
        if (!input) return;
        input.addEventListener("input", debounce(function () {
          state[pair[1]] = input.value.trim();
          renderAll();
        }, 150));
      });
  }

  function bindBatch() {
    document.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var batchBtn = t.closest("[data-batch]");
      if (batchBtn) {
        var parts = batchBtn.getAttribute("data-batch").split(":");
        ops.batch(parts[1], selectedIds(parts[0]), selectedPhones(parts[0]));
        return;
      }
      var clearBtn = t.closest("[data-batch-clear]");
      if (clearBtn) {
        state.sel[clearBtn.getAttribute("data-batch-clear")] = {};
        renderAll();
        return;
      }
    });
  }

  // 「添加账号」为页面级入口（模板中唯一一处），用事件委托覆盖所有 [data-add-account]，
  // 不依赖具体 id 或所在容器。
  function bindAdd() {
    document.addEventListener("click", function (e) {
      var t = e.target;
      if (t && t.closest && t.closest("[data-add-account]")) addAccount();
    });
  }

  function bindSelectAll() {
    Object.keys(GROUPS).forEach(function (group) {
      var box = $(GROUPS[group].all);
      if (!box) return;
      box.addEventListener("change", function () {
        var kw = state[group + "Search"];
        var filtered = groupAll(group).filter(function (a) { return accountMatch(a, kw); });
        state.sel[group] = {};
        if (box.checked) filtered.forEach(function (a) { state.sel[group][a.index] = true; });
        renderAll();
      });
    });
  }

  function bindCollapse() {
    // 三组通用折叠：button.acct-collapse[aria-expanded] + 由 aria-controls 指定的 .collapse-body
    //（高度动画由 CSS 的 grid-template-rows 承担）。按钮在模板中、不随表格重渲染，绑定一次即可。
    Array.prototype.forEach.call(document.querySelectorAll(".acct-collapse"), function (btn) {
      var body = document.getElementById(btn.getAttribute("aria-controls"));
      if (!body) return;
      btn.addEventListener("click", function () {
        var open = btn.getAttribute("aria-expanded") !== "true";
        btn.setAttribute("aria-expanded", String(open));
        body.classList.toggle("is-open", open);
      });
    });
  }

  /* ---------------- 容量 ---------------- */
  function capacityText(cur, max, breakdown) {
    var used = String(cur);
    if (!(Number(max) > 0)) return used + " / 不限";
    var parts = [];
    if (breakdown) {
      parts.push("正常 " + (breakdown.normal || 0));
      parts.push("用户暂停 " + (breakdown.user_paused || 0));
      parts.push("账密暂停 " + (breakdown.cred_paused || 0));
    }
    return used + " / " + max + (parts.length ? "（" + parts.join(" · ") + "）" : "");
  }

  function loadCapacity() {
    YB.api("GET", "/api/settings").then(function (d) {
      var c = (d && d.capacity) || {};
      setVal("capacity-accounts", capacityText(c.accounts, c.accounts_max, c.accounts_breakdown));
      setVal("capacity-users", capacityText(c.users, c.users_max, null));
      var e = (d && d.capacity_estimate) || {};
      setVal("capacity-estimate",
        "容量 " + (e.accounts_cap != null ? e.accounts_cap : "—") +
        " · 当前 " + (e.current_accounts != null ? e.current_accounts : "—") +
        " · 潜在 " + (e.potential_load != null ? e.potential_load : "—"));
    }).catch(function () {
      setVal("capacity-accounts", "加载失败");
      setVal("capacity-users", "加载失败");
      setVal("capacity-estimate", "加载失败");
    });
  }

  /* ---------------- 轮询 ---------------- */
  // 仅页面可见时刷新；有模态打开或写操作在途时跳过，避免打断用户操作与并发覆盖。
  function pollTick() {
    if (document.visibilityState !== "visible") return;
    if (state.busy) return;
    if (document.querySelector(".pm-backdrop")) return;
    // 行菜单打开时（含已 portal 到 body 的浮动态）不要重建行 DOM：会把 menu 的原父节点抽走。
    if (document.querySelector(".dd-wrap.is-open, .acct-menu--floating")) return;
    load(true);
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    bindSearch();
    bindBatch();
    bindAdd();
    bindSelectAll();
    bindCollapse();
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      load();
      loadCapacity();
      setInterval(pollTick, 10000);
      document.addEventListener("visibilitychange", function () { pollTick(); });
      // 行操作在 ≤900 为图标下拉、宽屏为并列按钮：跨断点需要重建行 DOM。
      if (window.matchMedia) {
        var narrow = window.matchMedia("(max-width: 900px)");
        var onBreak = function () { if (state.accounts.length) renderAll(); };
        if (narrow.addEventListener) narrow.addEventListener("change", onBreak);
        else if (narrow.addListener) narrow.addListener(onBreak);
      }
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
