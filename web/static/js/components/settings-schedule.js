/* 系统设置 · 签到调度分区（管理端 /settings）。

   挂载到 window.YB.settingsSchedule；classic script。分区按**逐字段权限**复刻后端
   POST /api/settings 的内联判定（app.py:6982-6989）：
     · 仅主管理员：sign_order / sign_dist / edge_front_sec / edge_back_sec /
       sign_window / gap_max / allow_time_pref
     · 任意管理员：saturday_sign / sunday_sign
   非主管理员：主管理员专属控件全部 disabled + 就地说明（可见而不改），
   周六/周日开关保持可用且**改动即保存**；主管理员的全部字段走显式保存。

   显式保存（主管理员）：改动只标脏（.badge--warn 脏标记 + 保存按钮出现），点
   「保存调度设置」才提交；脏时离开页面触发 beforeunload 守卫。提交只发送实际改动的
   字段；其中 gap_max 属容量硬门，额外走 YB.openConfirmPasswordModal 收集管理员口令。

   接口契约（不得改）：GET /api/settings 回填；POST /api/settings 部分更新。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var DEFAULTS = { order: "sequence", dist: "uniform", edge: 60, start: "06:30", end: "07:50" };

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
  function setDisabled(id, v) { var el = $(id); if (el) el.disabled = !!v; }
  function setTip(text, bad) {
    var n = $("ss-tip");
    if (!n) return;
    n.textContent = text || "";
    n.className = bad ? "set-tip set-bad" : "set-tip";
  }
  // 账号间隔上界 3600 与后端钳位一致：避免前端按未钳位值提示"已保存"而后端静默改小
  function clampGap(v) {
    v = parseInt(v, 10);
    if (isNaN(v)) return 0;
    return Math.min(3600, Math.max(0, v));
  }
  function quickButtons() {
    return [].slice.call(document.querySelectorAll("#set-schedule .btn-group [data-ss-edge]"));
  }
  function isMaster() { return !!(ctx && ctx.isMaster); }

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

  // 快捷值按钮的选中态：前后裁剪值一致时才点亮对应档位
  function syncQuickActive() {
    var f = edgeVal("ss-edge-front"), b = edgeVal("ss-edge-back");
    quickButtons().forEach(function (btn) {
      var sec = parseInt(btn.getAttribute("data-ss-edge"), 10);
      btn.classList.toggle("is-active", f === b && sec === f);
    });
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

  // 逐字段权限：主管理员专属控件在非主管理员下禁用（周六/周日始终可用）。
  function applyPerm() {
    var master = isMaster();
    ["ss-order", "ss-dist", "ss-edge-front", "ss-edge-back", "ss-gap",
     "ss-window-start", "ss-window-end", "ss-time-pref", "ss-reset", "ss-save"].forEach(function (id) {
      setDisabled(id, !master);
    });
    quickButtons().forEach(function (b) { b.disabled = !master; });
    setHidden($("ss-perm"), master);
    setHidden($("ss-save"), !master || !dirty);
    setHidden($("ss-dirty"), !master || !dirty);
    var card = $("set-schedule");
    if (card) {
      if (master) {
        card.removeAttribute("role");
        card.removeAttribute("aria-describedby");
      } else {
        card.setAttribute("role", "group");
        card.setAttribute("aria-describedby", "ss-perm");
      }
    }
  }

  /* ---------------- 主管理员：显式保存 ---------------- */
  function collect() {
    var body = {};
    var order = ($("ss-order") || {}).value || DEFAULTS.order;
    var dist = ($("ss-dist") || {}).value || DEFAULTS.dist;
    var f = edgeVal("ss-edge-front"), b = edgeVal("ss-edge-back");
    var gap = clampGap(num($("ss-gap"), snap ? snap.gap : 0));
    var pref = $("ss-time-pref") && $("ss-time-pref").checked ? 1 : 0;
    var sat = $("ss-sat") && $("ss-sat").checked ? 1 : 0;
    var sun = $("ss-sun") && $("ss-sun").checked ? 1 : 0;
    var win = windowParts();
    var winStr = win[0] + " ~ " + win[1];
    if (order !== snap.order) body.sign_order = order;
    if (dist !== snap.dist) body.sign_dist = dist;
    if (f !== snap.edgeFront) body.edge_front_sec = f;
    if (b !== snap.edgeBack) body.edge_back_sec = b;
    if (pref !== snap.pref) body.allow_time_pref = pref;
    if (sat !== snap.sat) body.saturday_sign = sat;
    if (sun !== snap.sun) body.sunday_sign = sun;
    if (winStr !== snap.window) body.sign_window = winStr;
    if (gap !== snap.gap) body.gap_max = gap;
    return body;
  }

  function snapshotFromDom() {
    return {
      order: ($("ss-order") || {}).value || DEFAULTS.order,
      dist: ($("ss-dist") || {}).value || DEFAULTS.dist,
      edgeFront: edgeVal("ss-edge-front"),
      edgeBack: edgeVal("ss-edge-back"),
      gap: clampGap(num($("ss-gap"), 0)),
      pref: $("ss-time-pref") && $("ss-time-pref").checked ? 1 : 0,
      sat: $("ss-sat") && $("ss-sat").checked ? 1 : 0,
      sun: $("ss-sun") && $("ss-sun").checked ? 1 : 0,
      window: windowParts().join(" ~ ")
    };
  }

  function afterSave() {
    snap = snapshotFromDom();
    clearDirty();
  }

  function save() {
    if (saving || !isMaster()) return;
    var body = collect();
    if (!Object.keys(body).length) { YB.toast.info("没有需要保存的改动"); return; }
    var needPw = Object.prototype.hasOwnProperty.call(body, "gap_max");
    var submit = function (pw) {
      if (pw) body.confirm_password = pw;
      saving = true;
      setTip("保存中…", false);
      setDisabled("ss-save", true);
      YB.api("POST", "/api/settings", body).then(function (data) {
        afterSave();
        setTip((data && data.msg) || "调度设置已保存", false);
        if (ctx.onSaved) ctx.onSaved();
      }).catch(function (e) {
        setTip((e && e.message) || "保存失败，请稍后重试", true);
      }).then(function () {
        saving = false;
        setDisabled("ss-save", false);
        applyPerm();
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

  function reset() {
    if (!isMaster()) return;
    YB.confirmDialog({
      title: "恢复默认调度",
      body: "恢复为：窗口 06:30 ~ 07:50 · 掐头去尾各 1 分钟 · 排序顺序 · 分布均匀。\n（账号间隔与自选开关不在恢复范围，可点保存生效）",
      confirmText: "恢复默认"
    }).then(function (ok) {
      if (!ok) return;
      var o = $("ss-order"), d = $("ss-dist");
      if (o) o.value = DEFAULTS.order;
      if (d) d.value = DEFAULTS.dist;
      setEdge("ss-edge-front", DEFAULTS.edge);
      setEdge("ss-edge-back", DEFAULTS.edge);
      var s = $("ss-window-start"), e = $("ss-window-end");
      if (s) s.value = DEFAULTS.start;
      if (e) e.value = DEFAULTS.end;
      syncQuickActive();
      updateEdgeWarn();
      markDirty();
    });
  }

  /* ---------------- 任意管理员：周末开关改动即保存 ---------------- */
  function saveWeekend(id, field, revert) {
    if (saving) { if (revert) revert(); return; }
    saving = true;
    var el = $(id);
    var body = {};
    body[field] = el && el.checked ? 1 : 0;
    setTip("保存中…", false);
    YB.api("POST", "/api/settings", body).then(function () {
      if (snap) snap[field === "saturday_sign" ? "sat" : "sun"] = body[field];
      setTip("已保存（下次自动签到时生效）", false);
    }).catch(function (e) {
      if (revert) revert();
      setTip((e && e.message) || "保存失败，请稍后重试", true);
    }).then(function () { saving = false; });
  }

  /* ---------------- 绑定 ---------------- */
  function bind() {
    ["ss-order", "ss-dist", "ss-window-start", "ss-window-end"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", function () {
        updateEdgeWarn(); markDirty();
      });
    });
    ["ss-edge-front", "ss-edge-back"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", function () {
        var v = Math.min(5, Math.max(0, parseFloat(el.value) || 0));
        el.value = String(Math.round(v * 2) / 2);
        syncQuickActive(); updateEdgeWarn(); markDirty();
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
    quickButtons().forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (btn.disabled) return;
        var sec = parseInt(btn.getAttribute("data-ss-edge"), 10);
        if (!isFinite(sec)) return;
        setEdge("ss-edge-front", sec);
        setEdge("ss-edge-back", sec);
        syncQuickActive(); updateEdgeWarn(); markDirty();
      });
    });
    // 周六/周日：主管理员并入显式保存；非主管理员（保存按钮不可用）改动即保存
    [["ss-sat", "saturday_sign"], ["ss-sun", "sunday_sign"]].forEach(function (pair) {
      var cb = $(pair[0]);
      if (!cb) return;
      cb.addEventListener("change", function () {
        if (isMaster()) { markDirty(); return; }
        var on = cb.checked;
        saveWeekend(pair[0], pair[1], function () { cb.checked = !on; });
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

  // 用服务器数据回填（首次加载与保存后重拉共用）。
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
      sat: data.saturday_sign ? 1 : 0,
      sun: data.sunday_sign ? 1 : 0,
      window: data.sign_window || (DEFAULTS.start + " ~ " + DEFAULTS.end)
    };
    var o = $("ss-order"), d = $("ss-dist");
    if (o) o.value = snap.order;
    if (d) d.value = snap.dist;
    setEdge("ss-edge-front", snap.edgeFront);
    setEdge("ss-edge-back", snap.edgeBack);
    var gap = $("ss-gap");
    if (gap) gap.value = String(snap.gap);
    var pref = $("ss-time-pref");
    if (pref) pref.checked = !!snap.pref;
    var sat = $("ss-sat"), sun = $("ss-sun");
    if (sat) sat.checked = !!snap.sat;
    if (sun) sun.checked = !!snap.sun;
    var parts = String(snap.window).split("~");
    var s = $("ss-window-start"), e = $("ss-window-end");
    if (s) s.value = (parts[0] || DEFAULTS.start).trim().slice(0, 5) || DEFAULTS.start;
    if (e) e.value = (parts[1] || DEFAULTS.end).trim().slice(0, 5) || DEFAULTS.end;
    applyPerm();
    syncQuickActive();
    updateEdgeWarn();
    setTip("", false);
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
