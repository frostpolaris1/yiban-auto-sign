/* 「签到日历」页编排（用户端 /user/calendar 与管理端 /my/calendar 共用的唯一实现；
   /mine/calendar 只是后者的 302 旧路径，不在路由表里）。

   挂载到 window.YB.signCalendarView；classic script，公开面 mount(opts)。

   职责边界：本组件只做**页面编排**（身份守卫 → 拉账号 → 生成日历卡 → 调共享日历模块），
   月历本体一律由 static/js/calendar.js（window.SignCalendar）渲染；正文 markup 由
   partials/page_sign_calendar.html 渲染（两端同一份）。

   opts：
     role          期望角色（"user" / "admin"）；不匹配则跳 denyRedirect
     denyRedirect  角色不匹配时的跳转目标
     emptyHref     无生效账号时「去提交账号」的链接目标

   动态文本一律 textContent / YB.el 写入，不用 innerHTML 拼不可信数据。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var list = null;
  var logCard = null;
  var emptyHref = "/data/dashboard";
  var emptyText = "";

  function svg(name) {
    return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  }

  /* 当前签到状态（含排队位次）：签到状态属于"结果"信息，从账号页迁到此处 */
  function statusLine(a) {
    var s = a.state_status || "pending";
    if (s === "success" || s === "already") return { cls: "state-line--ok", text: "今日已完成签到" };
    if (s === "no_task") return { cls: "state-line--muted", text: "今日无需签到" };
    if (s === "skipped_window" || s === "skipped_norange") return { cls: "state-line--warn", text: "未在签到时段" };
    if (s === "failed") {
      return { cls: "state-line--bad", text: "今日签到失败" + (a.state_message ? "：" + a.state_message : "") };
    }
    if (s === "paused") return { cls: "state-line--bad", text: "账号密码异常，签到已暂停，请到「我的账号」修改密码" };
    if (s === "user_cancelled") return { cls: "state-line--bad", text: "已取消签到（可在「我的账号」恢复）" };
    if (s === "retrying") return { cls: "state-line--warn", text: "签到重试中" };
    // 待签：state_message 形如"计划 HH:MM"（自动错峰），其余情况不展示
    var plan = a.state_message && a.state_message.indexOf("计划") === 0 ? " · 今日" + a.state_message : "";
    return { cls: "state-line--muted", text: "待签到" + plan + " · 前方排队 " + a.queue_ahead + " 人" };
  }

  /* 一张账号日历卡：卡头是账号名 + 当前状态，卡体是共享日历挂载点 */
  function calCard(a, i) {
    var card = YB.el("section", { class: "card cal-card" });
    var head = YB.el("div", { class: "panel-head" });
    var row = YB.el("div", { class: "panel-head-row" });
    row.appendChild(YB.el("h2", { class: "panel-title", text: a.display_name }));
    head.appendChild(row);
    head.appendChild(YB.el("p", {
      class: "panel-sub",
      text: String(a.phone || "") + (a.phone_model ? " · " + a.phone_model : "")
    }));
    var line = statusLine(a);
    head.appendChild(YB.el("p", { class: "panel-sub " + line.cls, text: line.text }));
    card.appendChild(head);
    var mount = YB.el("div", { class: "sc-mount" });
    mount.setAttribute("data-sc-mount", "");
    mount.setAttribute("data-sc-key", String(i));   // 仅用于本次挂载定位，不参与状态
    card.appendChild(mount);
    return card;
  }

  /* 无生效账号：给出明确去向（避免停在空日历上） */
  function emptyCard() {
    var card = YB.el("section", { class: "card" });
    var box = YB.el("div", { class: "empty" });
    box.appendChild(YB.el("span", { class: "empty__icon", html: svg("calendar") }));
    box.appendChild(YB.el("span", { class: "empty__msg", text: emptyText }));
    var action = YB.el("span", { class: "empty__action" });
    action.appendChild(YB.el("a", { class: "btn btn--primary", href: emptyHref, text: "去提交账号" }));
    box.appendChild(action);
    card.appendChild(box);
    return card;
  }

  function render() {
    var grid = list.closest(".user-grid");   // then/catch 两分支共用（失败态也要收 is-solo）
    YB.api("GET", "/api/my-accounts").then(function (data) {
      var active = ((data && data.accounts) || []).filter(function (a) {
        return !a.deleted && a.status === "active";
      });
      list.innerHTML = "";
      if (!active.length) {
        if (logCard) logCard.hidden = true;   // 没有日历就没有日志可看
        // 日志面板隐藏后左列独占整行：否则空卡片只占 6 列、右侧留 6 列死区
        if (grid) grid.classList.add("is-solo");
        list.appendChild(emptyCard());
        return;
      }
      if (logCard) logCard.hidden = false;
      if (grid) grid.classList.remove("is-solo");
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
      // 账号列表失败：错误文案 + 重试入口（同 my-accounts 组件「借空态位给错误态」的形态），
      // 并按空态口径收起日志面板 —— 没有日历就没有可查看的记录。
      list.innerHTML = "";
      if (logCard) logCard.hidden = true;
      if (grid) grid.classList.add("is-solo");
      var card = YB.el("section", { class: "card" });
      var box = YB.el("div", { class: "empty" });
      box.appendChild(YB.el("span", { class: "empty__msg", text: "账号列表加载失败，请稍后重试。" }));
      var action = YB.el("span", { class: "empty__action" });
      action.appendChild(YB.el("button", {
        type: "button", class: "btn btn--ghost btn--sm", text: "重试",
        onclick: function () { render(); },
      }));
      box.appendChild(action);
      card.appendChild(box);
      list.appendChild(card);
      YB.toast.error(e.message);
    });
  }

  function mount(opts) {
    opts = opts || {};
    list = document.querySelector("[data-cal-list]");
    logCard = document.querySelector("[data-sc-log-card]");
    if (!list) return;
    emptyHref = opts.emptyHref || (YB.BASE + "/data/dashboard");
    emptyText = opts.emptyText || "还没有生效的账号。提交账号并通过管理员审核后，这里会显示签到日历。";
    var logout = document.querySelector("[data-user-logout]");
    if (logout) logout.addEventListener("click", function () { YB.doLogout(); });

    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      if (opts.role && me.role !== opts.role) {
        location.href = opts.denyRedirect || (YB.BASE + "/data/dashboard");
        return;
      }
      render();
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }

  YB.signCalendarView = { mount: mount };
})();
