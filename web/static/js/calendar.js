/* 签到日历（**全站唯一实现**，用户自助页与旧管理端「我的账号」共用）。
   由 tests/test_web_calendar_parity.py 钉住"只此一份"：任何页面都不得再内联拼日期格。

   依赖 core.js 的公开面（YB.api / YB.$ / YB.escapeHtml / YB.toast）与页面上注入的
   Lucide 精灵图（<use href="#i-…">）。classic script，靠 window.renderCalendar 暴露给
   调用方（旧管理端模块按裸全局名调用，故此处不改成 ESM）。

   挂载约定：调用方在 DOM 里准备一个 `id="cal-wrap-<key>"` 的容器，
   再调 renderCalendar(phone, key)：
     · phone —— 内存状态键（月份游标按手机号存，避免列表排序变化导致错位）；
     · key   —— DOM 查询键（索引，避免手机号进 id）。
   容器内的交互一律走事件委托（监听挂在容器上，innerHTML 重绘不丢监听）。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;
  var esc = YB.escapeHtml;

  var WEEK = ["一", "二", "三", "四", "五", "六", "日"];
  var months = {};                    // phone -> {year, month}
  var state = { sundaySign: false, saturdaySign: false };  // 来自 /api/my-calendar 的停签开关

  function svg(name, cls) {
    return '<svg class="' + (cls || "cal-ico") + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
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

  // ==== 签到日历 · 日期格（唯一实现，勿在页面里再写一份）====
  // 通道分配按本项目的**信息层级**：
  //   ① 底色 = 签到结果（用户最想扫读的信息，给最强的视觉通道）：
  //            ✅ 成功 / ❌ 失败 / 周末停签（中性）；色值见 app.css 的 --cal-* 令牌，
  //            由 tests/test_web_text_contrast.py 按"文字 on 格底"实测 AA。
  //   ② inset ring = 今天（不占布局、与底色互不争夺，可叠加）；
  //   ③ 角标 = 「休」（周末停签），不单靠颜色区分。
  // ⚠ 周末停签**不**把数字做很浅：该格仍可点（点了提示「周末无需签到」），属有信息的
  //   格子、不是 WCAG 1.4.3 豁免的非活动控件，故同样用底色表达并保证可读。
  function calDayCell(o) {
    var cls = "cal-cell";
    if (o.off) cls += " cal-cell--off";
    else if (o.state === "✅") cls += " cal-cell--ok";
    else if (o.state === "❌") cls += " cal-cell--bad";
    else cls += " cal-cell--none";
    if (o.isToday) cls += " cal-cell--today";
    var off = o.offDay ? "（周" + o.offDay + "不签到）" : "";
    var label = o.date + (o.isToday ? "，今天" : "")
      + (o.state === "✅" ? "，已签到" : o.state === "❌" ? "，签到失败" : (off ? "" : "，查看签到记录"))
      + off;
    var badge = o.offDay ? '<span class="cal-off-badge" aria-hidden="true">休</span>' : "";
    return '<button type="button" class="' + cls + '" data-cal-date="' + esc(o.date) + '"'
      + ' title="' + esc(o.date) + '" aria-label="' + esc(label) + '">'
      + '<span class="cal-num">' + o.d + "</span>" + badge + "</button>";
  }

  function renderCalendar(phone, key) {
    var wrap = $("cal-wrap-" + key);
    if (!wrap) return;
    wrap.dataset.calKey = key;
    wrap.dataset.calPhone = phone;
    var st = monthOf(phone);
    var year = st.year, month = st.month;
    var monthStr = year + "-" + pad(month);
    wrap.innerHTML =
      '<div class="cal">'
      + '<div class="cal-main">'
      + '<div class="cal-head">'
      + '<h3 class="cal-title">签到日历 · ' + year + "年" + month + "月</h3>"
      + '<div class="cal-nav">'
      + '<button type="button" class="btn btn--ghost btn--icon" data-cal-shift="-1" aria-label="上个月">' + svg("chevron-left") + "</button>"
      + '<button type="button" class="btn btn--ghost btn--icon" data-cal-shift="1" aria-label="下个月">' + svg("chevron-right") + "</button>"
      + "</div></div>"
      + '<div class="cal-weekdays" aria-hidden="true">'
      + WEEK.map(function (w) { return "<span>" + w + "</span>"; }).join("")
      + "</div>"
      + '<div class="cal-grid" data-cal-grid aria-busy="true"></div>'
      + "</div>"
      + '<div class="cal-log" data-cal-log aria-live="polite"></div>'
      + "</div>";
    var grid = wrap.querySelector("[data-cal-grid]");
    YB.api("GET", "/api/my-calendar?month=" + monthStr).then(function (data) {
      state.sundaySign = !!data.sunday_sign;          // 管理员开启后周日照常显示/可查
      state.saturdaySign = data.saturday_sign === 1;  // 默认关闭（v0.29.0 起），开启后周六照常
      var html = "";
      var firstDay = (new Date(year, month - 1, 1).getDay() + 6) % 7;  // 周一起始
      var days = new Date(year, month, 0).getDate();
      for (var i = 0; i < firstDay; i++) html += '<span class="cal-blank"></span>';
      var today = todayStr();
      for (var d = 1; d <= days; d++) {
        var date = monthStr + "-" + pad(d);
        var cell = data.days && data.days[date] ? data.days[date][phone] || "" : "";
        var wd = new Date(year, month - 1, d).getDay();
        var sunOff = wd === 0 && !state.sundaySign;
        var satOff = wd === 6 && !state.saturdaySign;
        var off = sunOff || satOff;
        html += calDayCell({
          d: d, date: date, state: cell, off: off,
          offDay: off ? (sunOff ? "日" : "六") : "", isToday: date === today,
        });
      }
      grid.innerHTML = html;
      grid.removeAttribute("aria-busy");
    }).catch(function () {
      grid.innerHTML = '<p class="cal-error">日历加载失败，请稍后重试</p>';
      grid.removeAttribute("aria-busy");
    });
  }

  function loadDayLog(wrap, date) {
    var box = wrap.querySelector("[data-cal-log]");
    if (!box) return;
    // 周六/周日无需签到（各自开关关闭时）直接提示，不查日志
    var wd = new Date(date + "T00:00:00").getDay();
    if ((wd === 0 && !state.sundaySign) || (wd === 6 && !state.saturdaySign)) {
      box.innerHTML = '<p class="cal-log-empty">' + (wd === 0 ? "周日" : "周六") + "无需签到</p>";
      return;
    }
    box.innerHTML = '<p class="cal-log-empty">' + svg("loader", "cal-ico cal-spin") + " 加载中…</p>";
    YB.api("GET", "/api/my-logs?date=" + date).then(function (data) {
      if (!data.logs || !data.logs.length) {
        box.innerHTML = '<p class="cal-log-empty">' + esc(date) + " 暂无签到记录</p>";
        return;
      }
      box.innerHTML = '<p class="cal-log-head">' + esc(date) + " 签到记录（" + data.logs.length + " 条）</p>"
        + '<pre class="log-view">' + esc(data.logs.join("\n")) + "</pre>";
    }).catch(function (err) {
      box.innerHTML = "";
      YB.toast(err && err.message ? err.message : "日志加载失败", true);
    });
  }

  // 事件委托挂在容器上：innerHTML 整体重绘后监听依然有效，也免去内联 onclick 的全局函数
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var nav = t.closest("[data-cal-shift]");
    var cell = nav || t.closest("[data-cal-date]");
    if (!cell) return;
    var wrap = cell.closest("[data-cal-key]");
    if (!wrap) return;
    var phone = wrap.dataset.calPhone, key = wrap.dataset.calKey;
    if (nav) {
      monthOf(phone).month += Number(nav.getAttribute("data-cal-shift"));
      var st = monthOf(phone);
      if (st.month < 1) { st.month = 12; st.year--; }
      if (st.month > 12) { st.month = 1; st.year++; }
      renderCalendar(phone, key);
      return;
    }
    loadDayLog(wrap, cell.getAttribute("data-cal-date"));
  });

  // 兼容旧管理端模块调用的裸全局名（classic script 共享作用域）
  window.renderCalendar = renderCalendar;
  window.YBCal = { render: renderCalendar, dayCell: calDayCell };
})();
