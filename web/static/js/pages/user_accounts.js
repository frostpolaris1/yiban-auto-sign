/* 用户端「账号与设置」页（/user）。

   正文由 partials/page_my_accounts.html 渲染；行为编排在共享组件
   components/my-accounts-page.js（与管理端 /mine 同一份实现）。
   本文件只声明本页的分叉参数：角色守卫、日历链接、可自助注销。 */
(function () {
  "use strict";

  var YB = window.YB;
  if (!YB) return;

  function init() {
    YB.myAccountsPage.mount({
      role: "user",
      calendarHref: YB.BASE + "/user/calendar",
      denyRedirect: YB.BASE + "/"        // 管理员回后台
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
