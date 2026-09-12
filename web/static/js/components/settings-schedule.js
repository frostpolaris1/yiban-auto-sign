/* 系统设置 · 签到调度卡（管理端 /settings）。
   挂载到 window.YB.settingsSchedule；classic script。仅主管理员可改（后端 403 兜底），
   非主管理员时控件全部 disabled + 就地给出原因（可见而不改，便于理解权限）。

   显式保存：改动只标脏（.set-dirty + 保存/重置按钮出现），点「保存调度设置」才提交；
   脏时离开页面触发 beforeunload 守卫。提交**只发送实际改动的字段**（后端按字段携带写入），
   其中 gap_max 属容量硬门，额外走 YB.openConfirmPasswordModal 收集当前管理员口令。

   接口契约（不得改）：GET /api/settings 回填；POST /api/settings 部分更新，
   字段 sign_order / sign_dist / edge_front_sec / edge_back_sec / allow_time_pref /
   sign_window / gap_max（+ confirm_password）。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var DEFAULTS = { order: "sequence", dist: "uniform", edge: 60, start: "06:30", end: "07:50" };
  var EDGES = [0, 30, 60, 120, 300]; // 秒：0 / 0.5 / 1 / 2 / 5 分钟

  var ctx = null;
  var snap = null;          // 服务器快照（用于只提交改动字段）
  var dirty = false;
  var saving = false;

  function $(id) { return document.getElementById(id); }
  function num(el, fallback) {
    var v = parseInt(el && el.value, 10);
    return isNaN(v) ? fallback : v;
  }
  function edgeVal(id) {
    var v = parseFloat($(id) && $(id).value);
    if (isNaN(v)) return 0;
    v = Math.min(5, Math.max(0, v));
    return Math.round(v * 2) / 2 * 60; // 0.5 分钟对齐后转秒
  }
  function setEdge(id, sec) {
    var el = $(id);
    if (el) el.value = String((Math.round(sec / 30) * 30) / 60);
  }
  function windowParts() {
    var s = ($("ss-window-start") || {}).value || DEFAULTS.start;
    var e = ($("ss-window-end") || {}).value || DEFAULTS.end;
    return [s, e];
  }
  function windowSec() {
    var p = windowParts();
    var a = p[0].split(":"), b = p[1].split(":");
    var s = parseInt(a[0], 10) * 3600 + parseInt(a[1], 10) * 60;
    var e = parseInt(b[0], 10) * 3600 + parseInt(b[1], 10) * 60;
    return e > s ? e - s : 0;
  }
  function capacityCount() {
    var cap = ctx && ctx.capacity ? ctx.capacity() : null;
    var n = cap && Number(cap.current_accounts);
    return isFinite(n) && n > 0 ? n : 0;
  }

  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  // 账号间隔上界 3600 与后端钳位一致：避免前端按未钳位值提示"已保存"而后端静默改小
  function clampGap(v) {
    v = parseInt(v, 10);
    if (isNaN(v)) return 0;
    return Math.min(3600, Math.max(0, v));
  }

  function markDirty() {
    if (dirty) return;
    dirty = true;
    setHidden($("ss-save"), false);
    setHidden($("ss-dirty"), false);
  }
  function clearDirty() {
    dirty = false;
    setHidden($("ss-save"), true);
    setHidden($("ss-dirty"), true);
  }

  // 窗口容量警示：掐头去尾为 0 时边缘账号可能超时；窗口扣除掐头去尾与
  // 间隔×账号数后不足时，提示可能签不上。纯展示，不阻断保存。
  function updateEdgeWarn() {
    var warn = $("ss-edge-warn");
    if (!warn) return;
    var f = edgeVal("ss-edge-front"), b = edgeVal("ss-edge-back");
    var gap = clampGap(num($("ss-gap"), snap ? snap.gap : 0));
    var win = windowSec();
    var n = capacityCount();
    var msgs = [];
    if (f === 0 || b === 0) msgs.push("掐头或去尾为 0：对应边缘时段的账号可能超时");
    if (win > 0) {
      var need = f + b + Math.max(gap, 0) * Math.max(n, 1);
      if (need > win) {
        msgs.push("按当前账号数（" + n + "）与间隔估算需要约 " + Math.ceil(need / 60) +
          " 分钟，超过窗口 " + Math.round(win / 60) + " 分钟，部分账号可能签不上");
      }
    }
    warn.textContent = msgs.join("；");
    warn.hidden = msgs.length === 0;
  }

  function syncWindowStatic() {
    var p = windowParts();
    var el = $("set-window-static");
    if (el) el.textContent = p[0] + " ~ " + p[1];
  }

  // 非主管理员：可见但不可改，原因就地可见（不是隐藏）。
  function applyPerm() {
    var disabled = !(ctx && ctx.isMaster);
    ["ss-order", "ss-dist", "ss-edge-front", "ss-edge-back", "ss-gap",
     "ss-window-start", "ss-window-end", "ss-time-pref", "ss-reset", "ss-save"].forEach(function (id) {
      var el = $(id);
      if (el) el.disabled = disabled;
    });
    var chips = document.querySelectorAll("#set-schedule .set-chip");
    [].forEach.call(chips, function (b) { b.disabled = disabled; });
    setHidden($("ss-perm"), !disabled);
    // 整卡禁用：容器做 group 并把禁用原因 #ss-perm 关联给读屏（可见但不改的权限呈现）
    var card = $("set-schedule");
    if (card) {
      if (disabled) {
        card.setAttribute("role", "group");
        card.setAttribute("aria-describedby", "ss-perm");
      } else {
        card.removeAttribute("role");
        card.removeAttribute("aria-describedby");
      }
    }
  }

  function collect() {
    var body = {};
    var order = ($("ss-order") || {}).value || DEFAULTS.order;
    var dist = ($("ss-dist") || {}).value || DEFAULTS.dist;
    var f = edgeVal("ss-edge-front"), b = edgeVal("ss-edge-back");
    var gap = clampGap(num($("ss-gap"), snap ? snap.gap : 0));
    var pref = $("ss-time-pref") && $("ss-time-pref").checked ? 1 : 0;
    var win = windowParts();
    var winStr = win[0] + " ~ " + win[1];
    if (order !== snap.order) body.sign_order = order;
    if (dist !== snap.dist) body.sign_dist = dist;
    if (f !== snap.edgeFront) body.edge_front_sec = f;
    if (b !== snap.edgeBack) body.edge_back_sec = b;
    if (pref !== snap.pref) body.allow_time_pref = pref;
    if (winStr !== snap.window) body.sign_window = winStr;
    if (gap !== snap.gap) body.gap_max = gap;
    return body;
  }

  function afterSave() {
    snap = snapshotFromDom();
    clearDirty();
  }

  function snapshotFromDom() {
    return {
      order: ($("ss-order") || {}).value || DEFAULTS.order,
      dist: ($("ss-dist") || {}).value || DEFAULTS.dist,
      edgeFront: edgeVal("ss-edge-front"),
      edgeBack: edgeVal("ss-edge-back"),
      gap: clampGap(num($("ss-gap"), 0)),
      pref: $("ss-time-pref") && $("ss-time-pref").checked ? 1 : 0,
      window: windowParts().join(" ~ ")
    };
  }

  function post(body) {
    return YB.api("POST", "/api/settings", body);
  }

  function save() {
    if (saving) return;
    var body = collect();
    var keys = Object.keys(body);
    if (!keys.length) { YB.toast.info("没有需要保存的改动"); return; }
    if (!(ctx && ctx.isMaster)) { YB.toast.error("仅主管理员可修改调度设置"); return; }
    var needPw = Object.prototype.hasOwnProperty.call(body, "gap_max");
    var submit = function (pw) {
      if (pw) body.confirm_password = pw;
      saving = true;
      setDisabled($("ss-save"), true);
      post(body).then(function (data) {
        afterSave();
        YB.toast.success((data && data.msg) || "调度设置已保存");
        if (ctx.onSaved) ctx.onSaved();
      }).catch(function (e) {
        YB.toast.error((e && e.message) || "保存失败，请稍后重试");
      }).then(function () {
        saving = false;
        if (ctx && ctx.isMaster) setDisabled($("ss-save"), false);
      });
    };
    if (needPw) {
      YB.openConfirmPasswordModal(
        "调整账号间隔：不合适的设置可能影响签到成功率或被容量硬门拒绝。请输入当前管理员密码确认。",
        submit);
    } else {
      submit(null);
    }
  }

  function setDisabled(el, v) { if (el) el.disabled = !!v; }

  function reset() {
    YB.confirmDialog({
      title: "恢复默认调度",
      body: "恢复为：窗口 06:30 ~ 07:50 · 掐头去尾各 1 分钟 · 排序顺序 · 分布均匀。\n（账号间隔与自选开关不在恢复范围，可点保存生效）",
      confirmText: "恢复默认"
    }).then(function (ok) {
      if (!ok) return;
      setSelect("ss-order", DEFAULTS.order);
      setSelect("ss-dist", DEFAULTS.dist);
      setEdge("ss-edge-front", DEFAULTS.edge);
      setEdge("ss-edge-back", DEFAULTS.edge);
      var s = $("ss-window-start"), e = $("ss-window-end");
      if (s) s.value = DEFAULTS.start;
      if (e) e.value = DEFAULTS.end;
      syncWindowStatic();
      updateEdgeWarn();
      markDirty();
    });
  }
  function setSelect(id, v) { var el = $(id); if (el) el.value = v; }

  function bind() {
    ["ss-order", "ss-dist", "ss-window-start", "ss-window-end"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", function () {
        syncWindowStatic(); updateEdgeWarn(); markDirty();
      });
    });
    ["ss-edge-front", "ss-edge-back"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", function () {
        var v = Math.min(5, Math.max(0, parseFloat(el.value) || 0));
        el.value = String(Math.round(v * 2) / 2);
        updateEdgeWarn(); markDirty();
      });
    });
    var gap = $("ss-gap");
    if (gap) gap.addEventListener("change", function () {
      var v = clampGap(gap.value); // 本地按 3600 钳位，与后端静默钳位保持同一显示值
      gap.value = String(v);
      updateEdgeWarn(); markDirty();
    });
    var pref = $("ss-time-pref");
    if (pref) pref.addEventListener("change", markDirty);
    var chips = document.querySelectorAll("#set-schedule .set-chip");
    [].forEach.call(chips, function (btn) {
      btn.addEventListener("click", function () {
        if (btn.disabled) return;
        var sec = parseInt(btn.getAttribute("data-ss-edge"), 10);
        if (!isFinite(sec)) return;
        setEdge("ss-edge-front", sec);
        setEdge("ss-edge-back", sec);
        updateEdgeWarn(); markDirty();
      });
    });
    var saveBtn = $("ss-save");
    if (saveBtn) saveBtn.addEventListener("click", save);
    var resetBtn = $("ss-reset");
    if (resetBtn) resetBtn.addEventListener("click", reset);
    window.addEventListener("beforeunload", function (e) {
      if (!dirty) return;
      e.preventDefault();
      e.returnValue = "";
    });
  }

  // 用服务器数据回填（首次加载与保存后重拉共用）。仅当非脏时覆盖，避免抹掉用户输入。
  function apply(data) {
    snap = {
      order: data.sign_order || DEFAULTS.order,
      dist: data.sign_dist || DEFAULTS.dist,
      edgeFront: Number(data.edge_front_sec != null ? data.edge_front_sec
        : (data.window_edge_sec != null ? data.window_edge_sec : DEFAULTS.edge)),
      edgeBack: Number(data.edge_back_sec != null ? data.edge_back_sec
        : (data.window_edge_sec != null ? data.window_edge_sec : DEFAULTS.edge)),
      gap: Number(data.gap_max != null ? data.gap_max : 0),
      pref: data.allow_time_pref ? 1 : 0,
      window: data.sign_window || (DEFAULTS.start + " ~ " + DEFAULTS.end)
    };
    setSelect("ss-order", snap.order);
    setSelect("ss-dist", snap.dist);
    setEdge("ss-edge-front", snap.edgeFront);
    setEdge("ss-edge-back", snap.edgeBack);
    var gap = $("ss-gap");
    if (gap) gap.value = String(snap.gap);
    var pref = $("ss-time-pref");
    if (pref) pref.checked = !!snap.pref;
    var parts = String(snap.window).split("~");
    var s = $("ss-window-start"), e = $("ss-window-end");
    if (s) s.value = (parts[0] || DEFAULTS.start).trim().slice(0, 5) || DEFAULTS.start;
    if (e) e.value = (parts[1] || DEFAULTS.end).trim().slice(0, 5) || DEFAULTS.end;
    applyPerm();
    syncWindowStatic();
    updateEdgeWarn();
    clearDirty();
  }

  // 账号容量变化（保存容量上限/间隔后）时重算警示。
  function refreshWarn() { updateEdgeWarn(); }

  function mount(options) {
    ctx = options || {};
    bind();
    applyPerm();
  }

  YB.settingsSchedule = {
    mount: mount,
    apply: apply,
    refreshWarn: refreshWarn,
    isDirty: function () { return dirty; }
  };
})();
