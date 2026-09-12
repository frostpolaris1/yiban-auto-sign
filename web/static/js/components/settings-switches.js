/* 系统设置 · 系统开关分区（管理端 /settings）。

   挂载到 window.YB.settingsSwitches；classic script。整 tab 仅主管理员可见
   （后端 POST /api/settings 的 403 列表含 global_pause / registration_pause；
   页面 settings.js 对非主管理员隐藏 tab 按钮与面板）。

   两个开关都是危险语义：先 confirmDialog 写明影响范围，再由
   YB.openConfirmPasswordModal 收当前管理员口令，只提交被改的那一个字段。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var isMaster = false;
  var state = { globalPause: false, regPause: false };

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }

  function sync() {
    var gp = $("set-global-pause-text"), rp = $("set-reg-pause-text");
    if (gp) gp.textContent = state.globalPause ? "恢复自动签到" : "暂停自动签到";
    if (rp) rp.textContent = state.regPause ? "开放注册" : "暂停注册";
    var hint = $("set-pause-hint"), parts = [];
    if (state.globalPause) parts.push("签到当前处于暂停状态 — 自动签到不会执行，直至手动恢复");
    if (state.regPause) parts.push("注册当前处于暂停状态 — 新用户无法自助注册");
    if (hint) { hint.textContent = parts.join("；"); hint.hidden = parts.length === 0; }
  }

  function apply(data) {
    state.globalPause = !!(data && data.global_pause);
    state.regPause = !!(data && data.registration_pause);
    sync();
  }

  // 危险开关：确认写明影响范围 → 当前管理员口令 → 只提交被改的字段。
  function pauseAction(field, next) {
    if (!isMaster) return;
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
            sync();
            YB.toast.success(next ? what + "已暂停" : what + "已恢复");
          }).catch(function (e) {
            YB.toast.error((e && e.message) || "操作失败，请稍后重试");
          });
        });
    });
  }

  function mount(options) {
    isMaster = !!(options && options.isMaster);
    var gp = $("set-global-pause");
    if (gp) gp.addEventListener("click", function () { pauseAction("global_pause", !state.globalPause); });
    var rp = $("set-reg-pause");
    if (rp) rp.addEventListener("click", function () { pauseAction("registration_pause", !state.regPause); });
  }

  YB.settingsSwitches = { mount: mount, apply: apply };
})();
