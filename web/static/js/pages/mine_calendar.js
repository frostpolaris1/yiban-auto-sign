/* 管理端「签到日历」页（/mine/calendar）。

   正文由 partials/page_sign_calendar.html 渲染；行为编排在共享组件
   components/sign-calendar-view.js（与用户端 /user/calendar 同一份实现）。
   本文件只声明本页的分叉参数：角色守卫、空态去向。 */
(function () {
  "use strict";

  var YB = window.YB;
  if (!YB) return;

  function init() {
    YB.signCalendarView.mount({
      role: "admin",
      denyRedirect: YB.BASE + "/user",   // 普通用户回用户端
      emptyHref: YB.BASE + "/mine"
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
