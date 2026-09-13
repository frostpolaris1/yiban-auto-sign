/* 「我的账号」页编排（用户端 /user 与管理端 /mine 共用的唯一实现）。

   挂载到 window.YB.myAccountsPage；classic script，公开面 mount(opts)。

   两页正文由 partials/page_my_accounts.html 渲染（同一份 markup），本组件是与之配套的
   行为编排：调度提示条 + 身份守卫 + 侧栏邮箱 + 账号列表 + 邮件提醒 + 自选时段 +
   修改密码 +（可选）显示偏好 / 注销账号。可选区块由 **DOM 是否存在** 决定：
     · [data-delete-account]  仅用户端渲染 → 只有用户端绑注销
     · #owner-email-switch    仅管理端渲染 → 只有管理端绑归属邮箱偏好
   这样"同一份实现"不会因为分叉条件写死角色而漂移。

   opts：
     role            期望角色（"user" / "admin"）；不匹配则跳 denyRedirect
     calendarHref    生效账号卡的「签到日历」链接
     denyRedirect    角色不匹配时的跳转目标
     formVariant     传给 YB.accountForm 的 variant（默认 "user"）
   mail_notify 的变体按 me.is_builtin_admin 决定（主管理员走 /api/mail-config）。
   安全：零 innerHTML；动态文本一律 textContent / YB.el；写请求走 YB.api。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;

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

  /* ---------------- 注销账号（仅用户端渲染该按钮时绑定） ---------------- */
  function bindDeleteAccount() {
    var del = document.querySelector("[data-delete-account]");
    if (!del) return;
    del.addEventListener("click", function () {
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
    });
  }

  /* ---------------- 显示偏好（仅管理端渲染该开关时绑定） ---------------- */
  function bindOwnerEmail() {
    var cb = $("owner-email-switch");
    if (!cb) return;
    cb.checked = YB.prefs.ownerEmailVisible();
    cb.addEventListener("change", function () {
      YB.prefs.setOwnerEmailVisible(cb.checked);
      var tip = $("owner-email-tip");
      if (tip) tip.textContent = cb.checked ? "已开启（账号页下次渲染即生效）" : "已关闭";
    });
  }

  /* ---------------- 侧栏账号区（部分外壳才有） ---------------- */
  function renderSidebarEmail(me) {
    var email = me.email || "";
    Array.prototype.forEach.call(document.querySelectorAll("[data-account-email]"), function (n) {
      n.textContent = email.split("@")[0];
      n.title = email;
    });
  }

  function mount(opts) {
    opts = opts || {};
    var logout = document.querySelector("[data-user-logout]");
    if (logout) logout.addEventListener("click", function () { YB.doLogout(); });
    bindDeleteAccount();
    bindOwnerEmail();
    var pref = YB.timePref.mount();

    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      if (opts.role && me.role !== opts.role) {
        location.href = opts.denyRedirect || (YB.BASE + "/data/dashboard");
        return;
      }
      var isMaster = !!me.is_builtin_admin;
      var pwEntry = $("password-modal-btn");
      if (pwEntry) pwEntry.addEventListener("click", function () {
        YB.changePassword.open({ builtinAdmin: isMaster });
      });
      renderSidebarEmail(me);
      var accountsCtl = YB.myAccounts.mount({
        listSel: "account-list",
        emptySel: "accounts-empty",
        openBtnSel: "open-account-btn",
        formVariant: opts.formVariant || "user",
        calendarHref: opts.calendarHref || null,
        calendarMode: "link",
        // 管理端卡片补今日状态（今日已完成 / 前方排队 N 人）；用户端由日历页承担
        showState: opts.showState === true
      });
      YB.myMailNotify.mount({
        switchSel: "mail-notify-switch",
        tipSel: "mail-notify-tip",
        variant: isMaster ? "builtin-admin" : "user",
        initial: me.mail_notify
      });
      renderScheduleBanner(me);
      accountsCtl.reload();
      pref.load();
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }

  YB.myAccountsPage = { mount: mount };
})();
