/* 系统设置 · 系统开关分区（管理端 /work/settings）。

   挂载到 window.YB.settingsSwitches；classic script。整 tab 对非主管理员隐藏由页面
   settings.js 处理，但**那只是界面**，真正的门禁在后端。

   权限按**变更方向**分档（单源是 `web/app.py` 的 `MASTER_ONLY_KEYS` / `GATED_KEYS` /
   `GLOBAL_PAUSE_KEY`，本文件不再抄第二份键名清单）：
     · 急停（0→1）任意管理员都能做——把"先止损"的权力留在在场每个人手里；
     · 恢复（1→0）与注册开关的两个方向都仅主管理员——把"放开"的权力收在主管理员手里。
   所以「暂停」与「恢复」是**两颗不同权限的按钮**，不是一颗翻转钮：按当前状态只露出该露
   的那颗，无权限时禁用并就地说明（见 #sw-perm），不能让人点了没反应。

   每次动作都是：confirmDialog 写明影响范围 → 提交。急停（global_pause 0→1）不可逆，
   后端还会先要一次倒计时确认；其余方向只可能回口令。档位只存在于后端——本组件不判断、
   也不预判要不要口令，一律先不带凭据发，由响应体的 reason 决定补哪种凭据。
   state 仅成功分支更新，失败/取消时开关视觉状态保持原状。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var isMaster = false;
  var state = { globalPause: false, regPause: false };

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }

  // 这个方向当前会话能不能做（与后端 api_settings_save 的方向判定同口径）
  function canDo(field, next) {
    if (field === "global_pause" && !next) return isMaster;   // 恢复签到：仅主管理员（前端只决定按钮状态，不在此判权限）
    if (field !== "global_pause") return isMaster;             // 注册开关两个方向：仅主管理员
    return true;                                              // 急停签到：任意管理员
  }
  // 禁用原因**按按钮**取：急停那颗（global_pause 0→1）任意管理员可做、永不被禁，
  // 所以走这里的只有"恢复签到"与注册开关两类，不能整区一句"仅主管理员"
  // （那会把在场者唯一能做的止血动作一起否定）。
  function whyFor(field) {
    return field === "global_pause"
      ? "恢复自动签到仅主管理员可做"
      : "注册开关仅主管理员可做";
  }

  function sync() {
    var hint = $("set-pause-hint"), parts = [];
    if (state.globalPause) parts.push("签到当前处于暂停状态 — 自动签到不会执行，直至手动恢复");
    if (state.regPause) parts.push("注册当前处于暂停状态 — 新用户无法自助注册");
    if (hint) { hint.textContent = parts.join("；"); hint.hidden = parts.length === 0; }
    // 每个开关只露出"当前状态对应的下一步动作"那颗钮：暂停中给恢复、运行中给暂停。
    // 无权限的那颗禁用并就地说明原因，而不是留着让人点了没反应。
    var denied = false;
    [["global_pause", "set-gp", state.globalPause], ["registration_pause", "set-rp", state.regPause]]
      .forEach(function (row) {
        var field = row[0], base = row[1], paused = row[2];
        var pauseBtn = $(base + "-pause"), resumeBtn = $(base + "-resume");
        setHidden(pauseBtn, paused);          // 已暂停时不再提供「暂停」
        setHidden(resumeBtn, !paused);        // 运行中时不再提供「恢复」
        [[pauseBtn, true], [resumeBtn, false]].forEach(function (pair) {
          var b = pair[0]; if (!b) return;
          var allowed = canDo(field, pair[1]);
          b.disabled = !allowed;
          if (allowed) { b.removeAttribute("title"); b.removeAttribute("aria-describedby"); }
          else {
            b.title = whyFor(field);
            b.setAttribute("aria-describedby", "sw-perm");   // 就地说明（#sw-perm）+ 读屏可及
            denied = true;
          }
        });
      });
    // 有按钮被禁才露出权限说明；与 sh-perm / set-exec-perm 同构。
    setHidden($("sw-perm"), !denied);
  }

  function apply(data) {
    state.globalPause = !!(data && data.global_pause);
    state.regPause = !!(data && data.registration_pause);
    sync();
  }

  function applyPauseResult(field, next, what) {
    if (field === "global_pause") state.globalPause = next;
    else state.regPause = next;
    sync();
    YB.toast.success(next ? what + "已暂停" : what + "已恢复");
  }

  // 危险开关：确认写明影响范围 → 提交（只提交被改的那一个字段）。
  // 凭据交给统一 helper：先不带凭据发，后端按档位与风控回 reason 才补口令或倒计时确认。
  function pauseAction(field, next) {
    if (!canDo(field, next)) return;          // 方向级兜底：按钮已禁用，这里防 DOM 篡改
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
      var body = {};
      body[field] = next ? 1 : 0;
      YB.dangerousSubmit({
        path: "/api/settings", body: body,
        desc: "确认" + (next ? "暂停" : "恢复") + what + "？请输入当前管理员密码确认。",
        // 倒计时确认只对不可逆的急停出现；其余方向后端不会下发 delay_ack_required
        delayDesc: field === "global_pause" && next
          ? "暂停签到会让所有账号停止自动签到（正在运行的一轮会跑完），确认继续？" : null
      }).then(function () { applyPauseResult(field, next, what); })
        .catch(function (e) {
          if (e && e.canceled) return;   // 取消弹窗：不是失败
          YB.toast.error((e && e.message) || "操作失败，请稍后再试");
        });
    });
  }

  function mount(options) {
    isMaster = !!(options && options.isMaster);
    [["set-gp-pause", "global_pause", true], ["set-gp-resume", "global_pause", false],
     ["set-rp-pause", "registration_pause", true], ["set-rp-resume", "registration_pause", false]]
      .forEach(function (row) {
        var b = $(row[0]);
        if (b) b.addEventListener("click", function () { pauseAction(row[1], row[2]); });
      });
    sync();
  }

  YB.settingsSwitches = { mount: mount, apply: apply };
})();
