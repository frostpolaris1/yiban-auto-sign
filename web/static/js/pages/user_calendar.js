/* 用户端「签到日历」页（/user/calendar）行为。
   职责边界：本文件只做**页面编排**（拉账号 → 生成日历卡 → 调共享日历模块），
   月历本体一律由 static/js/calendar.js（window.SignCalendar）渲染。

   依赖 core.js：YB.api / YB.identity / YB.toast / YB.el / YB.doLogout。
   动态文本一律 textContent / YB.el 写入，不用 innerHTML 拼不可信数据。 */
(function () {
  "use strict";

  var YB = window.YB;
  if (!YB) return;

  var list = document.querySelector("[data-cal-list]");
  var logCard = document.querySelector("[data-sc-log-card]");

  function iconUse(name) {
    return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  }

  /* 一张账号日历卡：卡头是账号名 + 脱敏信息，卡体是共享日历挂载点 */
  function calCard(a, i) {
    var card = YB.el("section", { class: "card cal-card" });
    var head = YB.el("div", { class: "panel-head" });
    var text = YB.el("div", { class: "panel-head-text" });
    text.appendChild(YB.el("h2", { class: "panel-title", text: a.display_name }));
    text.appendChild(YB.el("p", {
      class: "panel-sub",
      text: String(a.phone || "") + " · 已生效"
    }));
    head.appendChild(text);
    card.appendChild(head);
    var mount = YB.el("div", { class: "sc-mount" });
    mount.setAttribute("data-sc-mount", "");
    mount.setAttribute("data-sc-key", String(i));   // 仅用于本次挂载定位，不参与状态
    card.appendChild(mount);
    return card;
  }

  /* 无生效账号：给出明确去向（避免用户停在空日历上） */
  function emptyCard() {
    var card = YB.el("section", { class: "card" });
    var box = YB.el("div", { class: "empty" });
    box.appendChild(YB.el("span", { class: "empty__icon", html: iconUse("calendar") }));
    box.appendChild(YB.el("span", {
      class: "empty__msg",
      text: "还没有生效的易班账号。提交账号并通过管理员审核后，这里会显示签到日历。"
    }));
    var action = YB.el("span", { class: "empty__action" });
    action.appendChild(YB.el("a", { class: "btn btn--primary", href: YB.BASE + "/user", text: "去提交账号" }));
    box.appendChild(action);
    card.appendChild(box);
    return card;
  }

  function render() {
    YB.api("GET", "/api/my-accounts").then(function (data) {
      var active = ((data && data.accounts) || []).filter(function (a) {
        return !a.deleted && a.status === "active";
      });
      list.innerHTML = "";
      if (!active.length) {
        if (logCard) logCard.hidden = true;   // 没有日历就没有日志可看
        list.appendChild(emptyCard());
        return;
      }
      if (logCard) logCard.hidden = false;
      var mounts = [];
      active.forEach(function (a, i) {
        list.appendChild(calCard(a, i));
        mounts.push({ el: list.querySelector('[data-sc-key="' + i + '"]'), phone: a.phone });
      });
      // 容器已入 DOM 后再渲染（render 内部按元素挂载，不依赖 id 查询）
      mounts.forEach(function (m) {
        if (m.el) window.SignCalendar.render(m.el, m.phone);
      });
    }).catch(function (e) {
      list.innerHTML = "";
      var card = YB.el("section", { class: "card" });
      card.appendChild(YB.el("p", { class: "empty__msg", text: "账号列表加载失败，请稍后重试。" }));
      list.appendChild(card);
      YB.toast.error(e.message);
    });
  }

  function bindStatic() {
    var logout = document.querySelector("[data-user-logout]");
    if (logout) logout.addEventListener("click", function () { YB.doLogout(); });
  }

  function init() {
    bindStatic();
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      if (me.role !== "user") { location.href = YB.BASE + "/"; return; }  // 管理员回后台
      var email = me.email || "";
      Array.prototype.forEach.call(document.querySelectorAll("[data-account-email]"), function (n) {
        n.textContent = email.split("@")[0];
        n.title = email;
      });
      render();
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
