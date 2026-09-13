/* 管理端「我的账号」页（/mine）。

   正文由 partials/page_my_accounts.html 渲染；行为编排在共享组件
   components/my-accounts-page.js（与用户端 /user 同一份实现）。
   本文件只声明本页的分叉参数：角色守卫、日历链接、卡片补今日状态。 */
(function () {
  "use strict";

  var YB = window.YB;
  if (!YB) return;

  function init() {
    YB.myAccountsPage.mount({
      role: "admin",
      calendarHref: YB.BASE + "/my/calendar",
      denyRedirect: YB.BASE + "/user/account",   // 普通用户回用户端
      showState: true                    // 卡内补今日状态（今日已完成 / 前方排队 N 人）
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
