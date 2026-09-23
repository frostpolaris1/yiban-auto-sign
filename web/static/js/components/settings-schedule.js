/* 系统设置 · 签到调度分区（管理端 /work/settings）。

   挂载到 window.YB.settingsSchedule；classic script。**本文件不抄键名档位清单**——档位由
   后端 `web/app.py` 的 `MASTER_ONLY_KEYS`（A 档：仅主管理员 + 当次口令）/ `GATED_KEYS`
   （B 档：任意管理员 + 口令，可短时豁免）单源决定，另有按方向分权的 `GLOBAL_PAUSE_KEY`；
   对拍测试会读这里的字面量与那两个常量比对，所以字段名保持 `body.<键> = …` 的直写形态。
   非主管理员：A 档控件禁用并就地说明（可见而不改），B 档可改。

   保存语义（与全页统一）：改动只标脏（脏徽标 + 保存按钮出现），点「保存调度设置」才
   提交；提交只发送相对服务器快照真正变化的字段，故非主管理员即便点保存也只送得出 B 档
   字段。**有改动就一律过口令框**：A 档必须当次口令，B 档同样要口令（只是可被短时豁免），
   多问一次不会错、少问必然 403。脏时离开页面由 settings.js 统一守卫（保存 / 放弃 / 取消）。

   对外面：apply(data) 回填、save() → Promise<boolean>（false = 取消或失败，页面据此
   决定不跳转）、isDirty()、markLeaving()、refreshWarn()。

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
  var leaving = false;      // 页面已就"离开"征得用户同意（由 markLeaving() 置位）
  var fallbackText = "";    // 窗口不可用（已回退默认）时服务端给的可见提示，正常为空串

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
  // 缓冲单边上限（分钟）：窗口宽度的 20%，且不超过既有量程 5 分钟；按 30s 粒度向下取整。
  // 与服务端 `yiban.window.edge_cap_sec` 同一条式子（粒度也必须一致，否则前端允许的值
  // 服务端会夹小、用户看到"保存后数字变了"）。
  function edgeMaxMin(winSec) {
    if (!(winSec > 0)) return 5;
    var cap = Math.floor(winSec * 0.2 / 30) * 30 / 60;
    return Math.max(0, Math.min(5, cap));
  }
  function capacityCount() {
    var cap = ctx && ctx.capacity ? ctx.capacity() : null;
    var n = cap && Number(cap.current_accounts);
    return isFinite(n) && n > 0 ? n : 0;
  }

  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  // aria-describedby 只增删本组件关心的 id，保留控件原有说明（如滑块触发器的当前值 id）。
  function associate(el, id, on) {
    if (!el || !el.getAttribute) return;
    var ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    var i = ids.indexOf(id);
    if (on && i === -1) ids.push(id);
    else if (!on && i !== -1) ids.splice(i, 1);
    if (ids.length) el.setAttribute("aria-describedby", ids.join(" "));
    else el.removeAttribute("aria-describedby");
  }
  // 禁用要落到"可见控件"上：自研控件与原生控件分离出可聚焦代理（下拉/滑块触发器、
  // 时间区间的 pair 触发器；原生控件是它自己），隐藏 input 上置 disabled 既不可见
  // 也不阻断交互。descId（权限说明）关联到该代理，读屏才能听到"为什么禁用"。
  function proxyOf(id) {
    var root = document.querySelector('[data-select-field="' + id + '"]');
    if (root) return root.querySelector(".select-trigger");
    root = document.querySelector('[data-range-field="' + id + '"]');
    if (root) return root.querySelector(".range-trigger");
    var tf = document.querySelector('[data-time-field="' + id + '"]');
    if (tf) {
      var host = tf.closest ? (tf.closest("[data-time-pair]") || tf) : tf;
      return host.querySelector(".time-trigger");
    }
    return $(id);
  }
  // descId 只在"权限禁用"时传入；保存中等瞬时禁用不关联权限说明。
  function setDisabled(id, v, descId) {
    var el = $(id);
    if (!el) return;
    var kind = document.querySelector('[data-select-field="' + id + '"]');
    if (kind && YB.selectField) {
      YB.selectField.setDisabled(id, v);
    } else {
      var range = document.querySelector('[data-range-field="' + id + '"]');
      if (range && YB.rangeField) {
        YB.rangeField.setDisabled(id, v);
      } else {
        var tf = document.querySelector('[data-time-field="' + id + '"]');
        if (tf && YB.timeField && YB.timeField.setDisabled) {
          // 时间字段：禁用交给组件落到触发器上（区间两个 id 共用一个触发器）
          YB.timeField.setDisabled(id, v);
        } else {
          var proxy = proxyOf(id);
          if (proxy && proxy !== el) proxy.disabled = !!v;   // 兜底：触发器挂在其隐藏 input 之外
          el.disabled = !!v;
        }
      }
    }
    if (descId) associate(proxyOf(id), descId, v);
  }
  function setTip(text, bad) { YB.setTip("ss-tip", text, bad); }
  // 账号间隔上界 3600 与后端钳位一致：避免前端按未钳位值提示"已保存"而后端静默改小
  function clampGap(v) {
    v = parseInt(v, 10);
    if (isNaN(v)) return 0;
    return Math.min(3600, Math.max(0, v));
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

  // 滑块可用上限随当前窗口动态收窄：窗口变小后旧值可能超出上限，保存会被服务端夹小，
  // 故先把量程改小（弹窗里的数字框/滑杆随即只能选到上限），再由 updateEdgeWarn 说明。
  function updateEdgeLimits() {
    var maxMin = edgeMaxMin(windowSec());
    ["ss-edge-front", "ss-edge-back"].forEach(function (id) {
      var root = document.querySelector('[data-range-field="' + id + '"]');
      if (root) root.setAttribute("data-max", String(maxMin));
    });
    return maxMin;
  }

  // 窗口容量警示：掐头去尾超过窗口的 20%（单边上限随窗口动态收窄，与服务端夹取同一
  // 规则）时提示会被自动收缩；窗口扣除掐头去尾与间隔×账号数后不足时，提示可能签不上。
  // 纯展示，不阻断保存。
  function updateEdgeWarn() {
    var warn = $("ss-edge-warn");
    if (!warn) return;
    var maxMin = updateEdgeLimits();
    var f = edgeVal("ss-edge-front"), b = edgeVal("ss-edge-back");
    var gap = clampGap(num($("ss-gap"), snap ? snap.gap : 0));
    var win = windowSec();
    var n = capacityCount();
    var msgs = [];
    if (fallbackText) msgs.push(fallbackText);
    if (win > 0 && (f > maxMin * 60 + 1e-9 || b > maxMin * 60 + 1e-9)) {
      msgs.push("缓冲超过窗口的 20%（单边上限 " + maxMin + " 分钟），保存时会被自动收缩");
    }
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
     "ss-window-start", "ss-window-end", "ss-time-pref"].forEach(function (id) {
      setDisabled(id, !master, "ss-perm");
    });
    setDisabled("ss-reset", !master, "ss-perm");
    setHidden($("ss-perm"), master);
    setHidden($("ss-save"), !dirty);
    setHidden($("ss-dirty"), !dirty);
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

  function submit(body, pw) {
    if (pw) body.confirm_password = pw;
    saving = true;
    setTip("保存中…", false);
    setDisabled("ss-save", true);
    return YB.api("POST", "/api/settings", body).then(function (data) {
      snap = snapshotFromDom();
      clearDirty();
      setTip((data && data.msg) || "调度设置已保存", false);
      if (ctx.onSaved) ctx.onSaved(data);
      return true;
    }, function (e) {
      setTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      saving = false;
      setDisabled("ss-save", false);
      applyPerm();
      return ok;
    });
  }

  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 用户取消 / 提交失败。
  function save() {
    if (saving) return Promise.resolve(false);
    var body = collect();
    if (!Object.keys(body).length) {
      clearDirty();
      YB.toast.info("没有需要保存的改动");
      return Promise.resolve(true);
    }
    // A 档必须当次口令，B 档同样要口令（只是可被短时豁免）——本文件不判档，
    // 一律先收口令再提交：多问一次不会错，少问必然 403。
    return new Promise(function (resolve) {
      YB.openConfirmPasswordModal(
        "调度参数改动会影响全站何时签到（窗口、掐头去尾、账号间隔等），不合适的设置可能拉低成功率或被容量硬门拒绝。请输入当前管理员密码确认。",
        function (pw) { submit(body, pw).then(resolve); },
        function () { resolve(false); });      // 取消口令 = 本次不保存
    });
  }

  function reset() {
    if (!isMaster()) return;
    YB.confirmDialog({
      title: "恢复默认调度",
      body: "恢复为：窗口 06:30 ~ 07:50 · 掐头去尾各 1 分钟 · 排序顺序 · 分布均匀。\n（账号间隔与自选开关不在恢复范围，可点保存生效）",
      confirmText: "恢复默认"
    }).then(function (ok) {
      if (!ok) return;
      YB.selectField.set("ss-order", DEFAULTS.order);
      YB.selectField.set("ss-dist", DEFAULTS.dist);
      YB.rangeField.set("ss-edge-front", 1);
      YB.rangeField.set("ss-edge-back", 1);
      YB.timeField.set("ss-window-start", DEFAULTS.start);
      YB.timeField.set("ss-window-end", DEFAULTS.end);
      updateEdgeWarn();
      markDirty();
    });
  }

  function bind() {
    ["ss-order", "ss-dist", "ss-window-start", "ss-window-end"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", function () {
        updateEdgeWarn(); markDirty();
      });
    });
    // 滑块值由 range-field 在弹窗确认后回写并派发 change（取消不留痕，不标脏）
    ["ss-edge-front", "ss-edge-back"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", function () { updateEdgeWarn(); markDirty(); });
    });
    var gap = $("ss-gap");
    if (gap) {
      gap.addEventListener("change", function () {
        var v = clampGap(gap.value); // 本地按 3600 钳位，与后端静默钳位保持同一显示值
        gap.value = String(v);
        updateEdgeWarn(); markDirty();
      });
      gap.addEventListener("input", updateEdgeWarn);
    }
    var pref = $("ss-time-pref");
    if (pref) pref.addEventListener("change", markDirty);
    // 周六/周日/自选：改动只标脏，随「保存调度设置」一并提交（非主管理员只有前两个可改）
    ["ss-sat", "ss-sun", "ss-time-pref"].forEach(function (id) {
      var cb = $(id);
      if (cb) cb.addEventListener("change", markDirty);
    });
    var saveBtn = $("ss-save");
    if (saveBtn) saveBtn.addEventListener("click", function () { save(); });
    var resetBtn = $("ss-reset");
    if (resetBtn) resetBtn.addEventListener("click", reset);
    // 兜底守卫：关闭标签页/刷新。站内跳转由页面脚本的确认弹窗接管（见 pages/work_settings.js），
    // 用户确认离开后由 markLeaving() 放行，避免二次拦截。
    window.addEventListener("beforeunload", function (e) {
      if (!dirty || leaving) return;
      e.preventDefault();
      e.returnValue = "";
    });
  }

  // 用服务器数据回填（首次加载与保存后重拉共用）。
  function apply(data) {
    fallbackText = data && data.window_fallback_text
      ? String(data.window_fallback_text) : "";
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
    YB.selectField.set("ss-order", snap.order);
    YB.selectField.set("ss-dist", snap.dist);
    YB.rangeField.set("ss-edge-front", snap.edgeFront / 60);
    YB.rangeField.set("ss-edge-back", snap.edgeBack / 60);
    var gap = $("ss-gap");
    if (gap) gap.value = String(snap.gap);
    var pref = $("ss-time-pref");
    if (pref) pref.checked = !!snap.pref;
    var sat = $("ss-sat"), sun = $("ss-sun");
    if (sat) sat.checked = !!snap.sat;
    if (sun) sun.checked = !!snap.sun;
    var parts = String(snap.window).split("~");
    YB.timeField.set("ss-window-start", (parts[0] || DEFAULTS.start).trim().slice(0, 5) || DEFAULTS.start);
    YB.timeField.set("ss-window-end", (parts[1] || DEFAULTS.end).trim().slice(0, 5) || DEFAULTS.end);
    applyPerm();
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
    save: save,
    refreshWarn: refreshWarn,
    isDirty: function () { return dirty; },
    markLeaving: function () { leaving = true; }
  };
})();
