/* 签到日历（**全站唯一实现**）：可复用的月历组件，挂在任意容器上。
   由 tests/test_web_calendar_parity.py 钉住"只此一份"：任何页面都不得再内联拼日期格。

   依赖 core.js 的公开面（YB.api / YB.$ / YB.escapeHtml / YB.toast）与页面注入的
   Lucide 精灵图（<use href="#i-…">）。classic script，公开面：
     · window.SignCalendar.render(mount, phone)  —— mount 为元素或元素 id；phone 为账号键
     · window.renderCalendar(mount, phone)       —— 兼容旧调用名（同 SignCalendar.render）

   ## 类名为什么是 sc-* 而不是 cal-*
   Adminator 自带一套事件月历（`.cal-grid` 有 grid-auto-rows:minmax(110px,1fr)、
   `.cal-cell` 带 border-right/bottom 与 flex-direction:column、`.mini-cal-*` 等）。
   本项目自研的是"按日状态 + 日志"的签到日历，与事件月历语义不同；沿用同名类会
   被 Adminator 的未覆盖属性渗透（曾导致日期格被撑成 62×110 的竖长条）。故一律用
   独立前缀 sc-（sign calendar），与设计系统零冲突。

   ## 容器约定
   · 月历挂在 `mount` 内（整个 mount 由本模块重建）；
   · 日志面板（可选）由页面渲染，本模块按
     `mount 内 [data-sc-log]` → `文档内 [data-sc-log]` 的顺序寻找，
     并在 `[data-sc-log-date]` 回显当前日期。没有日志面板时只渲染日历。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;
  var esc = YB.escapeHtml;

  var WEEK = ["一", "二", "三", "四", "五", "六", "日"];
  var months = {};                                   // phone -> {year, month}
  var flags = { sunday: false, saturday: false };    // 来自 /api/my-calendar 的停签开关

  function svg(name, cls) {
    return '<svg class="' + (cls || "sc-ico") + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  }
  function pad(n) { return String(n).padStart(2, "0"); }
  function todayStr() {
    var d = new Date();
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }
  function monthOf(phone) {
    if (!months[phone]) {
      var d = new Date();
      months[phone] = { year: d.getFullYear(), month: d.getMonth() + 1 };
    }
    return months[phone];
  }
  function logPanel(mount) {
    return mount.querySelector("[data-sc-log]") || document.querySelector("[data-sc-log]");
  }

  // ==== 签到日历 · 日期格（唯一实现，勿在页面里再写一份）====
  // 视觉通道分配（颜色之外必须有冗余编码，不单靠颜色传达状态）：
  //   ① 底色 + 圆点 = 签到结果（✅ 成功 / ❌ 失败）；色值见 app.css 的 --cal-* 令牌，
  //      由 tests/test_web_text_contrast.py 按"文字 on 格底"实测 AA；
  //   ② inset ring  = 今天（不占布局，与底色、选中框互不争夺）；
  //   ③ 角标「休」 = 周末停签（底色中性 + 文字角标，双重编码）；
  //   ④ getSelected = 选中（主色实框 + 主色浅底，独立于上面三者）。
  // ⚠ 周末停签**不**把数字做很浅：该格仍可点（点了提示「周末无需签到」），属有信息的
  //   格子、不是 WCAG 1.4.3 豁免的非活动控件，故同样保证可读。
  function dayCell(o) {
    var cls = "sc-cell";
    if (o.off) cls += " sc-cell--off";
    else if (o.state === "✅") cls += " sc-cell--ok";
    else if (o.state === "❌") cls += " sc-cell--bad";
    else cls += " sc-cell--none";
    if (o.isToday) cls += " sc-cell--today";
    if (o.selected) cls += " is-selected";
    var off = o.offDay ? "（周" + o.offDay + "不签到）" : "";
    var label = o.date + (o.isToday ? "，今天" : "")
      + (o.state === "✅" ? "，已签到" : o.state === "❌" ? "，签到失败" : (off ? "" : "，查看签到记录"))
      + off;
    var dot = o.state === "✅" ? '<i class="sc-dot sc-dot--ok" aria-hidden="true"></i>'
      : o.state === "❌" ? '<i class="sc-dot sc-dot--bad" aria-hidden="true"></i>' : "";
    var badge = o.offDay ? '<i class="sc-off" aria-hidden="true">休</i>' : "";
    return '<button type="button" class="' + cls + '" data-sc-date="' + esc(o.date) + '"'
      + ' title="' + esc(o.date) + '" aria-label="' + esc(label) + '"'
      + (o.selected ? ' aria-pressed="true"' : ' aria-pressed="false"') + '>'
      + '<span class="sc-num">' + o.d + "</span>" + dot + badge + "</button>";
  }

  function monthLabel(year, month) {
    return year + "年" + month + "月";
  }

  // selectDate（可选）：渲染完成后自动选中该日并回显日志。
  // 「今天」按钮必须走这条路径——render 是异步的（要等 /api/my-calendar），
  // 在 render 之后同步查格子必然查不到（曾因此点了"今天"只换月份不选中）。
  function render(mount, phone, selectDate) {
    if (typeof mount === "string") mount = $(mount);
    if (!mount) return;
    var st = monthOf(phone);
    var year = st.year, month = st.month;
    var monthStr = year + "-" + pad(month);
    var selected = selectDate || mount.getAttribute("data-sc-selected") || "";
    mount.setAttribute("data-sc-phone", phone);
    mount.innerHTML =
      '<div class="sc-toolbar">'
      + '<div class="sc-toolbar-left">'
      + '<h3 class="sc-month">' + monthLabel(year, month) + "</h3>"
      + '<div class="sc-nav">'
      + '<button type="button" class="btn btn--ghost btn--icon sc-nav-btn" data-sc-shift="-1" aria-label="上个月">' + svg("chevron-left") + "</button>"
      + '<button type="button" class="btn btn--ghost btn--icon sc-nav-btn" data-sc-shift="1" aria-label="下个月">' + svg("chevron-right") + "</button>"
      + "</div></div>"
      + '<button type="button" class="btn btn--ghost btn--sm" data-sc-today>今天</button>'
      + "</div>"
      + '<div class="sc-weekdays" aria-hidden="true">'
      + WEEK.map(function (w) { return "<span>" + w + "</span>"; }).join("")
      + "</div>"
      + '<div class="sc-grid" data-sc-grid aria-busy="true"></div>';
    var grid = mount.querySelector("[data-sc-grid]");
    YB.api("GET", "/api/my-calendar?month=" + monthStr).then(function (data) {
      flags.sunday = !!data.sunday_sign;          // 管理员开启后周日照常显示/可查
      flags.saturday = data.saturday_sign === 1;  // 默认关闭（v0.29.0 起），开启后周六照常
      var html = "";
      var firstDay = (new Date(year, month - 1, 1).getDay() + 6) % 7;  // 周一起始
      var days = new Date(year, month, 0).getDate();
      for (var i = 0; i < firstDay; i++) html += '<span class="sc-blank"></span>';
      var today = todayStr();
      for (var d = 1; d <= days; d++) {
        var date = monthStr + "-" + pad(d);
        var cell = data.days && data.days[date] ? data.days[date][phone] || "" : "";
        var wd = new Date(year, month - 1, d).getDay();
        var sunOff = wd === 0 && !flags.sunday;
        var satOff = wd === 6 && !flags.saturday;
        var off = sunOff || satOff;
        html += dayCell({
          d: d, date: date, state: cell, off: off, selected: date === selected,
          offDay: off ? (sunOff ? "日" : "六") : "", isToday: date === today,
        });
      }
      grid.innerHTML = html;
      grid.removeAttribute("aria-busy");
      if (selectDate) {
        mount.setAttribute("data-sc-selected", selectDate);
        loadLog(mount, selectDate);
      }
    }).catch(function () {
      grid.innerHTML = '<p class="sc-error">日历加载失败，请稍后重试</p>';
      grid.removeAttribute("aria-busy");
    });
  }

  /* ---------------- 日志面板 ---------------- */
  function logDate(text) {
    var el = document.querySelector("[data-sc-log-date]");
    if (el) el.textContent = text;
  }
  function logBody() { return document.querySelector("[data-sc-log]"); }

  // 与 pages/user_calendar.html 的服务端静态空态保持同一结构（图标 + 文案）
  function emptyHtml(text) {
    return '<p class="sc-empty"><span class="sc-empty-icon" aria-hidden="true">'
      + svg("calendar") + "</span><span>" + text
      + '</span><span class="sc-empty-note">签到结果会在每天调度完成后写入日志</span></p>';
  }

  function showLogPlaceholder() {
    var box = logBody();
    if (box) box.innerHTML = emptyHtml("点击左侧日历中的日期，这里会显示当天的签到记录");
    logDate("选择日期查看当天记录");
  }

  function loadLog(mount, date) {
    var box = logBody();
    if (!box) return;
    var wd = new Date(date + "T00:00:00").getDay();
    logDate(date);
    // 周六/周日无需签到（各自开关关闭时）直接提示，不查日志
    if ((wd === 0 && !flags.sunday) || (wd === 6 && !flags.saturday)) {
      box.innerHTML = emptyHtml((wd === 0 ? "周日" : "周六") + "无需签到");
      return;
    }
    box.innerHTML = '<p class="sc-empty">' + svg("loader", "sc-ico sc-spin") + " 加载中…</p>";
    YB.api("GET", "/api/my-logs?date=" + date).then(function (data) {
      if (!data.logs || !data.logs.length) {
        box.innerHTML = emptyHtml(esc(date) + " 暂无签到记录");
        return;
      }
      box.innerHTML = '<pre class="log-view sc-log-text">' + esc(data.logs.join("\n")) + "</pre>";
    }).catch(function (err) {
      box.innerHTML = emptyHtml("读取失败，请稍后重试");
      YB.toast.error(err && err.message ? err.message : "日志加载失败");
    });
  }

  function select(mount, cell) {
    var prev = mount.querySelector(".sc-cell.is-selected");
    if (prev) {
      prev.classList.remove("is-selected");
      prev.setAttribute("aria-pressed", "false");
    }
    cell.classList.add("is-selected");
    cell.setAttribute("aria-pressed", "true");
    var date = cell.getAttribute("data-sc-date");
    mount.setAttribute("data-sc-selected", date);
    loadLog(mount, date);
  }

  function gotoMonth(mount, phone, date) {
    var parts = date.split("-");
    var st = monthOf(phone);
    st.year = Number(parts[0]);
    st.month = Number(parts[1]);
    render(mount, phone, date);   // 选中交给渲染回调（异步完成后再落定）
  }

  // 事件委托挂在容器上：innerHTML 整体重绘后监听依然有效
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var host = t.closest("[data-sc-phone]");
    if (!host) return;
    var phone = host.getAttribute("data-sc-phone");
    if (t.closest("[data-sc-shift]")) {
      var delta = Number(t.closest("[data-sc-shift]").getAttribute("data-sc-shift"));
      var st = monthOf(phone);
      st.month += delta;
      if (st.month < 1) { st.month = 12; st.year--; }
      if (st.month > 12) { st.month = 1; st.year++; }
      var keep = host.getAttribute("data-sc-selected") || "";
      render(host, phone);
      var back = keep ? host.querySelector('[data-sc-date="' + keep + '"]') : null;
      if (back) select(host, back);
      return;
    }
    if (t.closest("[data-sc-today]")) {
      gotoMonth(host, phone, todayStr());
      return;
    }
    var cell = t.closest(".sc-cell[data-sc-date]");
    if (cell && !cell.disabled) select(host, cell);
  });

  window.SignCalendar = { render: render, placeholder: showLogPlaceholder };
  window.renderCalendar = render;   // 兼容旧调用名（pages/mine.js 等）
  // 页面里没有日志面板时不显示占位（由页面模板自带空态）
  document.addEventListener("DOMContentLoaded", function () {
    if (logBody() && !logBody().querySelector(".sc-empty, .log-view")) showLogPlaceholder();
  });
})();
