/* 用户端「签到日历」页（/user/calendar）。

   正文由 partials/page_sign_calendar.html 渲染；行为编排在共享组件
   components/sign-calendar-view.js（与管理端 /my/calendar 同一份实现）。
   本文件只声明本页的分叉参数：角色守卫、空态去向。 */
(function () {
  "use strict";

  var YB = window.YB;
  if (!YB) return;

  function init() {
    YB.signCalendarView.mount({
      role: "user",
      denyRedirect: YB.BASE + "/data/dashboard",       // 管理员回后台
      emptyHref: YB.BASE + "/user/account"
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
