/* 管理端「我的账号」页（/mine）行为编排。

   依赖 core.js 公开面与共享组件：
     YB.myAccounts（账号卡片列表，inline 模式挂共享日历）、YB.accountForm（账号表单）、
     YB.timePref（自选时段）、YB.myMailNotify（邮件提醒开关）、YB.changePassword（改密弹窗）。
   本文件只保留本页编排：身份/权限判定、显示偏好开关、组件装配。

   权限：邮件提醒开关按身份分两条后端路径 —— 主管理员走 /api/mail-config
   {admin_notify}（关闭需当前密码），注册管理员走 /api/my-mail-notify。二者由
   YB.myMailNotify 的 variant 决定，本页只负责传入 is_builtin_admin。

   安全：零 innerHTML；动态文本一律 textContent / YB.el；写请求走 YB.api（自带 CSRF）。 */
(function () {
  "use strict";

  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;

  var accountsCtl = null;

  /* ---------------- 显示偏好（本机 localStorage，不写服务器配置） ---------------- */
  function bindOwnerEmail() {
    var cb = $("mine-owner-email");
    if (!cb) return;
    cb.checked = YB.prefs.ownerEmailVisible();
    cb.addEventListener("change", function () {
      YB.prefs.setOwnerEmailVisible(cb.checked);
      var tip = $("mine-owner-email-tip");
      if (tip) tip.textContent = cb.checked ? "已开启（账号页下次渲染即生效）" : "已关闭";
    });
  }

  /* ---------------- 静态控件 ---------------- */
  function bindStatic(isMaster) {
    var pw = $("password-modal-btn");
    if (pw) pw.addEventListener("click", function () {
      YB.changePassword.open({ builtinAdmin: isMaster });
    });
  }

  /* ---------------- 装配 ---------------- */
  function init() {
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      if (me.role !== "admin") { location.href = YB.BASE + "/user"; return; }
      var isMaster = !!me.is_builtin_admin;
      bindStatic(isMaster);
      bindOwnerEmail();
      accountsCtl = YB.myAccounts.mount({
        listSel: "mine-account-list",
        emptySel: "mine-accounts-empty",
        openBtnSel: "mine-open-account-btn",
        formVariant: "user",       // owner 作用域端点 /api/my-accounts，与用户端同表单
        calendarMode: "inline",    // 生效账号卡内联签到日历（管理端无独立日历页）
        showState: true            // 卡内补今日状态（今日已完成 / 前方排队 N 人）
      });
      YB.myMailNotify.mount({
        switchSel: "mail-notify-switch",
        tipSel: "mail-notify-tip",
        variant: isMaster ? "builtin-admin" : "user",
        initial: me.mail_notify
      });
      YB.timePref.mount().load();
      accountsCtl.reload();
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
