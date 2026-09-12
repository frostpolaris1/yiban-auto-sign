/* 系统设置页（管理端 /settings）行为编排。
   依赖 core.js 与 components/{settings-schedule,settings-health,settings-notify,settings-mail}.js。

   职责：身份判定（is_builtin_admin）→ 拉取 GET /api/settings 回填概览/容量/周末/危险区 →
   挂载四张组件卡 → 公告读写、显示偏好、容量上限保存、危险区开关与改密。

   字段级权限（逐字段复刻后端内联判定；UI 禁用不是安全边界，高危请求仍带 confirm_password）：
     · 任意管理员：saturday_sign / sunday_sign / account_verify / probe_* / 公告 / 显示偏好
     · 仅主管理员：调度（组件内）/ 容量上限 / 推送 / 邮件（组件内）/ 危险区
   安全：全页零 innerHTML；不打印后端 e.data；写请求走 YB.api；改密走完整口令策略。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;
  var el = YB.el;

  var state = {
    isMaster: false, mailNotify: true, capacityEst: null,
    globalPause: false, regPause: false, capUsers: 0, capAccounts: 0
  };

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function setTip(id, text, bad) {
    var n = $(id);
    if (!n) return;
    n.textContent = text || "";
    n.className = bad ? "set-tip set-bad" : "set-tip";
  }
  function setHidden(node, hidden) { if (node) node.hidden = !!hidden; }

  /* ---------------- 页面级状态条（加载中 / 失败 + 重试） ---------------- */
  function setStatus(tone, text, retry) {
    var box = $("set-status");
    if (!box) return;
    box.classList.remove("info", "danger");
    box.classList.add(tone);
    var t = $("set-status-text");
    if (t) t.textContent = text;
    var btn = box.querySelector("[data-set-retry]");
    if (btn) btn.hidden = !retry;
    box.hidden = false;
  }
  function hideStatus() { var box = $("set-status"); if (box) box.hidden = true; }

  /* ---------------- 概览与容量 ---------------- */
  function renderCapacity(est, cap) {
    var box = $("set-capacity");
    if (!box) return;
    clear(box);
    if (!est) {
      box.className = "set-cap";
      box.appendChild(el("p", { class: "set-cap-sub", text: "暂无预估数据" }));
      return;
    }
    var cur = Number(est.current_accounts) || 0;
    var capN = Number(est.accounts_cap) || 0;
    var load = Number(est.potential_load) || 0;
    // cap=0（窗口退化到容纳不下一次签到）且已用>0 同样视为超限，不因 cap>0 短路漏报
    var over = cur > capN || cur + load > capN;
    box.className = "set-cap" + (over ? " set-cap--over" : "");
    box.appendChild(el("p", { class: "set-cap-title", text: "签到容量（按当前账号间隔估算）" }));
    box.appendChild(el("div", { class: "set-cap-main", text: "已用 " + cur + " / 容量 " + capN + " 个" }));
    box.appendChild(el("p", {
      class: "set-cap-sub",
      text: "另有 " + load + " 人已注册未提交（潜在负载）" + (over ? "；已超容量，保存更大的账号间隔将被拒绝" : "")
    }));
    if (cap && typeof cap === "object") {
      var bd = cap.accounts_breakdown || {};
      var used = Number(cap.accounts) || 0;
      var lim = Number(cap.accounts_max) || 0;
      box.appendChild(el("p", {
        class: "set-breakdown",
        text: "账号容量 " + (lim > 0 ? used + "/" + lim : used + "/不限") +
          "（正常 " + (Number(bd.normal) || 0) + " · 自暂停 " + (Number(bd.user_paused) || 0) +
          " · 账密故障暂停 " + (Number(bd.cred_paused) || 0) + "）"
      }));
      box.appendChild(el("p", {
        class: "set-cap-sub", text: "占额含自暂停与账密故障暂停账号；需释放名额可在账号管理页清理。"
      }));
    }
  }

  // 容量上限按权限启用/禁用；禁用时把原因 #set-cap-perm 与控件做程序化关联（读屏可及）。
  function setCapPerm(disabled) {
    ["set-max-users", "set-max-accounts", "set-cap-save"].forEach(function (id) {
      var n = $(id);
      if (!n) return;
      n.disabled = !!disabled;
      if (disabled) n.setAttribute("aria-describedby", "set-cap-perm");
      else n.removeAttribute("aria-describedby");
    });
    setHidden($("set-cap-perm"), !disabled);
  }

  function renderLimits(cap) {
    state.capUsers = Number(cap && cap.users_max) || 0;
    state.capAccounts = Number(cap && cap.accounts_max) || 0;
    var u = $("set-max-users"), a = $("set-max-accounts");
    if (u) u.value = String(state.capUsers);
    if (a) a.value = String(state.capAccounts);
    setCapPerm(!state.isMaster);
  }

  function capBox(text, withRetry) {
    var box = $("set-capacity");
    if (!box) return;
    clear(box);
    box.className = "set-cap";
    box.appendChild(el("p", { class: "set-cap-sub", text: text }));
    if (!withRetry) return;
    var btn = el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "重试" });
    btn.style.marginTop = "8px";
    btn.addEventListener("click", function () { startLoad().catch(function () {}); });
    box.appendChild(btn);
  }

  function saveCapacity() {
    var u = parseInt(($("set-max-users") || {}).value, 10);
    var a = parseInt(($("set-max-accounts") || {}).value, 10);
    if (isNaN(u)) u = state.capUsers;
    if (isNaN(a)) a = state.capAccounts;
    var body = {};
    if (u !== state.capUsers) body.max_users = u;
    if (a !== state.capAccounts) body.max_accounts = a;
    if (!Object.keys(body).length) { setTip("set-cap-tip", "没有需要保存的改动", false); return; }
    YB.openConfirmPasswordModal(
      "调整容量上限：不合适的设置可能影响新增注册/账号，是否继续？\n请输入当前管理员密码确认。",
      function (pw) {
        body.confirm_password = pw;
        setTip("set-cap-tip", "保存中…", false);
        YB.api("POST", "/api/settings", body).then(function (data) {
          setTip("set-cap-tip", (data && data.msg) || "容量上限已保存", false);
          return loadAll();
        }).catch(function (e) {
          setTip("set-cap-tip", (e && e.message) || "保存失败，请稍后重试", true);
        });
      });
  }

  /* ---------------- 公告 ---------------- */
  var annBusy = false;
  function loadAnnouncement() {
    return YB.api("GET", "/api/announcement").then(function (data) {
      var input = $("set-announcement");
      if (input) input.value = (data && data.text) || "";
    }).catch(function () { /* 公告读取失败不阻塞整页 */ });
  }
  function saveAnnouncement(text) {
    if (annBusy) return;
    annBusy = true;
    setTip("set-ann-tip", "保存中…", false);
    YB.api("PUT", "/api/announcement", { text: text }).then(function (data) {
      setTip("set-ann-tip", (data && data.msg) || (text ? "公告已更新" : "公告已清除"), false);
    }).catch(function (e) {
      setTip("set-ann-tip", (e && e.message) || "保存失败，请稍后重试", true);
    }).then(function () { annBusy = false; });
  }
  function bindAnnouncement() {
    var input = $("set-announcement");
    if (input) {
      // 后端禁换行：前端也拦住粘贴/输入的换行（避免提交后才 400）
      input.addEventListener("input", function () {
        var v = input.value.replace(/[\r\n\u2028\u2029]+/g, " ");
        if (v !== input.value) input.value = v;
      });
    }
    var save = $("set-ann-save");
    if (save) save.addEventListener("click", function () {
      saveAnnouncement((($("set-announcement") || {}).value || "").trim());
    });
    var clr = $("set-ann-clear");
    if (clr) clr.addEventListener("click", function () {
      var input2 = $("set-announcement");
      if (input2) input2.value = "";
      saveAnnouncement("");
    });
  }

  /* ---------------- 周末签到（任意管理员，改动即保存） ---------------- */
  function bindWeekend() {
    [["set-sunday", "sunday_sign"], ["set-saturday", "saturday_sign"]].forEach(function (pair) {
      var cb = $(pair[0]);
      if (!cb) return;
      cb.addEventListener("change", function () {
        var on = cb.checked, body = {};
        body[pair[1]] = on ? 1 : 0;
        setTip("set-weekend-tip", "保存中…", false);
        YB.api("POST", "/api/settings", body).then(function () {
          setTip("set-weekend-tip", "已保存（下次自动签到时生效）", false);
        }).catch(function (e) {
          cb.checked = !on;
          setTip("set-weekend-tip", (e && e.message) || "保存失败，请稍后重试", true);
        });
      });
    });
  }
  function syncWeekend(data) {
    var s = $("set-sunday"), t = $("set-saturday");
    if (s) s.checked = !!data.sunday_sign;
    if (t) t.checked = !!data.saturday_sign;
  }

  /* ---------------- 显示偏好（P10，浏览器级 localStorage） ---------------- */
  function bindDisplay() {
    var cb = $("set-owner-email");
    if (!cb) return;
    cb.checked = YB.prefs.ownerEmailVisible();
    cb.addEventListener("change", function () {
      YB.prefs.setOwnerEmailVisible(cb.checked);
      setTip("set-display-tip", cb.checked ? "已开启（账号页下次渲染即生效）" : "已关闭", false);
    });
  }

  /* ---------------- 危险区（仅主管理员） ---------------- */
  function syncDanger() {
    setHidden($("set-danger"), !state.isMaster);
    var gp = $("set-global-pause-text"), rp = $("set-reg-pause-text");
    if (gp) gp.textContent = state.globalPause ? "恢复自动签到" : "暂停自动签到";
    if (rp) rp.textContent = state.regPause ? "开放注册" : "暂停注册";
    var hint = $("set-pause-hint"), parts = [];
    if (state.globalPause) parts.push("签到当前处于暂停状态 — 自动签到不会执行，直至手动恢复");
    if (state.regPause) parts.push("注册当前处于暂停状态 — 新用户无法自助注册");
    if (hint) { hint.textContent = parts.join("；"); hint.hidden = parts.length === 0; }
  }

  // 危险开关：确认写明影响范围 → 当前管理员口令 → 只提交被改的字段。
  function pauseAction(field, next) {
    var what = field === "global_pause" ? "签到" : "注册";
    var impact = field === "global_pause"
      ? (next ? "所有账号将停止自动签到：正在运行的一轮会跑完，手动签到不受影响，可随时恢复。确认继续？"
        : "下一轮自动签到将恢复执行。确认继续？")
      : (next ? "登录页将关闭注册入口，新用户无法自助注册；已注册用户登录不受影响。确认继续？"
        : "登录页将恢复注册入口。确认继续？");
    YB.confirmDialog({
      title: (next ? "暂停" : "恢复") + what, body: impact,
      confirmText: next ? "暂停" : "恢复", danger: next
    }).then(function (ok) {
      if (!ok) return;
      YB.openConfirmPasswordModal(
        "确认" + (next ? "暂停" : "恢复") + what + "？请输入当前管理员密码确认。",
        function (pw) {
          var body = { confirm_password: pw };
          body[field] = next ? 1 : 0;
          YB.api("POST", "/api/settings", body).then(function () {
            if (field === "global_pause") state.globalPause = next;
            else state.regPause = next;
            syncDanger();
            YB.toast.success(next ? what + "已暂停" : what + "已恢复");
          }).catch(function (e) {
            YB.toast.error((e && e.message) || "操作失败，请稍后重试");
          });
        });
    });
  }

  function openPasswordForm() {
    var isMaster = state.isMaster;
    var oldIn = el("input", { id: "set-pw-old", class: "input", type: "password", autocomplete: "current-password", placeholder: "当前密码" });
    var newIn = el("input", { id: "set-pw-new", class: "input", type: "password", autocomplete: "new-password", placeholder: "新密码" });
    var cfmIn = el("input", { id: "set-pw-cfm", class: "input", type: "password", autocomplete: "new-password", placeholder: "再次输入新密码" });
    var err = el("div", { class: "field-error", role: "alert", hidden: true });
    var stack = el("div", { class: "set-pw-grid" });
    function field(label, input) {
      var f = el("div", { class: "set-field" });
      // label 必须 for 指向 input.id，否则点击/读屏都无法与控件建立关联
      f.appendChild(el("label", { class: "set-label", for: input.id, text: label }));
      f.appendChild(input);
      return f;
    }
    stack.appendChild(field("当前密码", oldIn));
    stack.appendChild(field("新密码（" + (isMaster ? YB.PW_ADMIN_HINT : YB.PW_POLICY_HINT) + "）", newIn));
    stack.appendChild(field("确认新密码", cfmIn));
    stack.appendChild(err);
    var submitting = false;
    YB.openModal({
      title: isMaster ? "修改主管理员密码" : "修改密码",
      body: stack,
      actions: [
        { label: "取消", variant: "ghost" },
        {
          label: "保存新密码", variant: "primary",
          onClick: function (handle) {
            if (submitting) return false; // 防重入：连点只发一次请求
            var oldPw = oldIn.value, password = newIn.value, confirm = cfmIn.value;
            // 完整策略判定：主管理员 12 位三类、普通 10 位两类；不得退化为裸长度比较
            var ok = isMaster ? YB.passwordPolicyOkAdmin(password) : YB.passwordPolicyOk(password);
            function clearInvalid() {
              [oldIn, newIn, cfmIn].forEach(function (n) { n.classList.remove("is-invalid"); });
            }
            // 出错：错误文案挂在 role=alert 节点，输入框标红并把焦点移回出错字段
            function reject(msg, input) {
              clearInvalid();
              err.textContent = msg;
              err.hidden = false;
              if (input) { input.classList.add("is-invalid"); input.focus(); }
              return false;
            }
            err.hidden = true;
            if (!ok) return reject("新密码" + (isMaster ? YB.PW_ADMIN_HINT : YB.PW_POLICY_HINT), newIn);
            if (!oldPw) return reject("请输入当前密码验证", oldIn);
            if (password !== confirm) return reject("两次输入的新密码不一致", cfmIn);
            submitting = true;
            var footBtns = handle.panel.querySelectorAll(".modal-foot .btn");
            [].forEach.call(footBtns, function (n) { n.disabled = true; });
            function release() {
              submitting = false;
              [].forEach.call(footBtns, function (n) { n.disabled = false; });
            }
            YB.api("POST", "/api/me/password", {
              old_password: oldPw, new_password: password, confirm_password: confirm
            }).then(function () {
              // 改密会递增主管理员口令版本，当前会话随即失效：明示并跳登录
              YB.toast.success("密码已更新，请重新登录");
              YB.closeModal(handle);
              setTimeout(function () { location.href = YB.BASE + "/login"; }, 1200);
            }).catch(function (e) {
              release();
              reject((e && e.message) || "修改失败，请稍后重试", null);
            });
            return false; // 校验/提交期间不自动关闭
          }
        }
      ]
    });
  }

  function bindDanger() {
    var gp = $("set-global-pause");
    if (gp) gp.addEventListener("click", function () { pauseAction("global_pause", !state.globalPause); });
    var rp = $("set-reg-pause");
    if (rp) rp.addEventListener("click", function () { pauseAction("registration_pause", !state.regPause); });
    var pw = $("set-pw-btn");
    if (pw) pw.addEventListener("click", openPasswordForm);
  }

  /* ---------------- 装配与加载 ---------------- */
  function applySettings(data) {
    state.capacityEst = data.capacity_estimate || null;
    state.globalPause = !!data.global_pause;
    state.regPause = !!data.registration_pause;
    renderCapacity(data.capacity_estimate, data.capacity);
    renderLimits(data.capacity);
    syncWeekend(data);
    syncDanger();
    YB.settingsSchedule.apply(data);
    YB.settingsHealth.apply(data);
  }

  // 核心设置加载：加载中显状态条，失败显「设置加载失败」+ 重试并把容量只读区置
  // 「加载失败」，成功隐藏状态条。只覆盖 GET /api/settings（notify/mail 由 startLoad 拉起，
  // 保存容量后的局部刷新不应重放这两张卡，以免抹掉用户未保存的编辑）。
  function loadAll() {
    setStatus("info", "正在加载设置…", false);
    capBox("正在加载容量统计…", false);
    return YB.api("GET", "/api/settings").then(function (data) {
      applySettings(data);
      hideStatus();
    }, function (e) {
      setStatus("danger", "设置加载失败", true);
      capBox("容量统计加载失败：" + ((e && e.message) || "请稍后重试"), true);
      YB.toast.error((e && e.message) || "设置加载失败，请稍后重试");
      throw e;
    });
  }

  // 首屏与页面级重试共用：核心设置 + 两个主管理员专属卡（各自失败就地提示 + 重试）
  function startLoad() {
    return loadAll().then(function () {
      YB.settingsNotify.load();
      YB.settingsMail.load();
    });
  }

  // 写操作成功后只刷新容量（不重放调度/健康字段，避免抹掉用户未保存的编辑）。
  function refreshCapacity() {
    return YB.api("GET", "/api/settings").then(function (data) {
      state.capacityEst = data.capacity_estimate || null;
      renderCapacity(data.capacity_estimate, data.capacity);
      renderLimits(data.capacity);
      YB.settingsSchedule.refreshWarn();
    }).catch(function () {});
  }

  function init() {
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      state.isMaster = !!me.is_builtin_admin;
      state.mailNotify = me.mail_notify != null ? !!me.mail_notify : true;
      // 身份判定后立即按权限禁用容量控件：renderLimits 要等 GET /api/settings 返回，
      // 期间存在「已渲染但未禁用」的可点击窗口（后端 403 兜底，此处收紧 UI）。
      setCapPerm(!state.isMaster);
      YB.settingsSchedule.mount({
        isMaster: state.isMaster,
        capacity: function () { return state.capacityEst; },
        onSaved: refreshCapacity
      });
      YB.settingsHealth.mount();
      YB.settingsNotify.mount({ isMaster: state.isMaster });
      YB.settingsMail.mount({ isMaster: state.isMaster, mailNotify: state.mailNotify });
      bindAnnouncement();
      bindWeekend();
      bindDisplay();
      bindDanger();
      var capSave = $("set-cap-save");
      if (capSave) capSave.addEventListener("click", saveCapacity);
      // 状态条重试走事件委托（按钮是模板静态节点，无需逐次绑定）
      document.addEventListener("click", function (e) {
        var t = e.target;
        if (t && t.closest && t.closest("[data-set-retry]")) startLoad().catch(function () {});
      });
      loadAnnouncement();
      startLoad().catch(function () {});
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
