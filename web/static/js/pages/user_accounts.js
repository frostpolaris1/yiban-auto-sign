/* 用户端「账号与设置」页（/user）行为。
   依赖 core.js 的公开面与共享组件：
     YB.myAccounts（账号卡片列表）、YB.myMailNotify（邮件开关）、
     YB.changePassword（改密弹窗）、YB.timePref（自选时段）、YB.accountForm（账号表单）。
   本文件只保留本页特有编排：调度提示条、注销账号、身份守卫。
   全部动态文本一律经 textContent / YB.el 写入，不用 innerHTML 拼不可信数据。 */
(function () {
  "use strict";

  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;

  var accountsCtl = null;

  /* ---------------- 调度提示条 ---------------- */
  function renderScheduleBanner(me) {
    var bar = document.querySelector("[data-schedule-banner]");
    var txt = document.querySelector("[data-schedule-banner-text]");
    if (!bar || !txt) return;
    var order = me.sign_order === "random" ? "每天随机安排" : "按固定顺序安排";
    var prefText = me.time_pref_allowed ? "可自选签到时间" : "暂不可自选签到时间";
    txt.textContent = "签到方式：" + order + " · 窗口 " + (me.sign_window || "") + " · " + prefText;
    bar.hidden = false;
  }

  /* ---------------- 注销账号（本页特有） ---------------- */
  function onDeleteAccount() {
    YB.confirmDialog({
      title: "注销账号",
      body: "注销将删除你的账号、易班账号与自选签到时间。7 天内可撤销恢复，超过 7 天将永久删除，无法找回。",
      confirmText: "继续注销", danger: true
    }).then(function (ok) {
      if (!ok) return;
      YB.openConfirmPasswordModal(
        "注销后账号将无法登录，易班账号与自选签到时间会被删除；7 天宽限期内可撤销。请输入当前密码完成注销。",
        function (pw) {
          YB.api("POST", "/api/me/delete", { password: pw }).then(function (data) {
            YB.toast.success(data.msg || "账号已注销");
            try { localStorage.clear(); } catch (e) { /* 受限环境忽略 */ }
            setTimeout(function () { location.href = YB.BASE + "/login"; }, 1200);
          }).catch(function (e) {
            // 文案由后端给出（400 密码不正确 / 403 / 429 请稍后再试 / 500）
            YB.toast.error(e.message || "注销失败，请稍后再试");
          });
        }
      );
    });
  }

  /* ---------------- 静态控件绑定 ---------------- */
  function bindStatic() {
    var logout = document.querySelector("[data-user-logout]");
    if (logout) logout.addEventListener("click", function () { YB.doLogout(); });

    var del = document.querySelector("[data-delete-account]");
    if (del) del.addEventListener("click", onDeleteAccount);

    var pwEntry = $("password-modal-btn");
    if (pwEntry) pwEntry.addEventListener("click", function () {
      YB.changePassword.open({ builtinAdmin: false });
    });
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    bindStatic();
    var pref = YB.timePref.mount();
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      if (me.role !== "user") { location.href = YB.BASE + "/"; return; }  // 管理员回后台
      var email = me.email || "";
      // 侧栏账号区只显示邮箱前缀，完整地址放 title（窄栏不撑破）
      Array.prototype.forEach.call(document.querySelectorAll("[data-account-email]"), function (n) {
        n.textContent = email.split("@")[0];
        n.title = email;
      });
      accountsCtl = YB.myAccounts.mount({
        listSel: "account-list",
        emptySel: "accounts-empty",
        openBtnSel: "open-account-btn",
        formVariant: "user",
        calendarHref: YB.BASE + "/user/calendar",
        calendarMode: "link"
      });
      YB.myMailNotify.mount({
        switchSel: "mail-notify-switch",
        tipSel: "mail-notify-tip",
        variant: "user",
        initial: me.mail_notify
      });
      renderScheduleBanner(me);
      accountsCtl.reload();
      pref.load();
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
