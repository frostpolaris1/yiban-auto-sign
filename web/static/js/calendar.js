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
  var autoLoadedDate = "";                           // 已自动加载过日志的日期（多账号共享日志面板时去重）

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
  var ROWS = 6;                 // 固定 6 行（42 格）：任意月份等高，换月不跳
  var CELLS = ROWS * 7;
  var ANIM_MIN_MS = 80;         // 短于此值的请求不播进入动画（加载越短越不该动）
  var SWAP_MS = YB.SWAP_MS || 160;   // 与 core.js 的 swapOut 同口径（退出窗口）
  // 用户偏好减少动效：完全不播（不加任何过渡类，而不是把时长归零）
  function reduceMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }
  function clearShift(node) {
    if (!node) return;
    node.classList.remove("is-swapping", "is-shifting-in", "is-shift-next", "is-shift-prev");
  }

  // 换月方向动效（P58）：旧格保持到数据到达后，再做一次有方向的「退出 → 换内容 → 进入」。
  // dir>0 往后一月（新内容自右侧进入、旧内容向左退），dir<0 反之。只动 transform/opacity：
  // 退出 ease-in-strong、进入 ease-out-strong（基类承担），各 SWAP_MS。位移由 CSS 按方向给出。
  // 点击反馈本身需要动，故不按请求快慢跳过；reduced-motion 下直接落内容。
  function shiftMonth(grid, label, dir, apply) {
    if (!dir || reduceMotion()) { clearShift(grid); clearShift(label); apply(); return; }
    var seq = (grid.__scShiftSeq || 0) + 1;
    grid.__scShiftSeq = seq;
    clearShift(grid); clearShift(label);
    grid.classList.add(dir > 0 ? "is-shift-next" : "is-shift-prev");
    grid.classList.add("is-swapping");
    if (label) label.classList.add("is-swapping");
    var done = false;
    function onEnd(e) { if (e.target === grid) enter(); }
    function enter() {
      if (done) return;
      done = true;
      grid.removeEventListener("transitionend", onEnd);
      if (grid.__scShiftSeq !== seq) return;     // 已被下一次切换取代
      apply();                                   // 换内容（此刻 opacity 仍为 0）
      grid.classList.remove("is-swapping");
      if (label) label.classList.remove("is-swapping");
      grid.classList.add("is-shifting-in");      // 进入起点：偏移到「来向」一侧（无过渡落位）
      if (label) label.classList.add("is-shifting-in");
      void grid.offsetWidth;                     // 提交起点，使摘类时产生进入过渡
      requestAnimationFrame(function () {
        grid.classList.remove("is-shifting-in", "is-shift-next", "is-shift-prev");
        if (label) label.classList.remove("is-shifting-in");
      });
    }
    grid.addEventListener("transitionend", onEnd);
    setTimeout(enter, SWAP_MS);
  }

  // 建壳（只建一次）：工具栏与星期表头不随月份重绘，之后只更新标题与网格内容
  function shell(mount) {
    var grid = mount.querySelector("[data-sc-grid]");
    if (grid) return grid;
    mount.innerHTML =
      '<div class="sc-toolbar">'
      + '<div class="sc-toolbar-left">'
      + '<h3 class="sc-month">—</h3>'
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
    return mount.querySelector("[data-sc-grid]");
  }

  // 首屏骨架格：与真实格同尺寸（静态，不做逐格动画）
  function skeletonHtml() {
    var out = "";
    for (var i = 0; i < CELLS; i++) {
      out += '<span class="sc-cell sc-cell--skeleton" aria-hidden="true"></span>';
    }
    return out;
  }

  // 固定 42 格：前置空位 + 当月日期 + 尾部补齐 → 每个月都是 6 行，卡片高度恒定
  function gridHtml(data, phone, year, month, monthStr, selected) {
    var firstDay = (new Date(year, month - 1, 1).getDay() + 6) % 7;  // 周一起始
    var days = new Date(year, month, 0).getDate();
    var today = todayStr();
    var cells = [];
    var lead;
    for (lead = 0; lead < firstDay; lead++) cells.push('<span class="sc-blank"></span>');
    for (var d = 1; d <= days; d++) {
      var date = monthStr + "-" + pad(d);
      var stt = data.days && data.days[date] ? data.days[date][phone] || "" : "";
      var wd = new Date(year, month - 1, d).getDay();
      var sunOff = wd === 0 && !flags.sunday;
      var satOff = wd === 6 && !flags.saturday;
      var off = sunOff || satOff;
      cells.push(dayCell({
        d: d, date: date, state: stt, off: off, selected: date === selected,
        offDay: off ? (sunOff ? "日" : "六") : "", isToday: date === today,
      }));
    }
    while (cells.length < CELLS) cells.push('<span class="sc-blank"></span>');
    return cells.join("");
  }

  function render(mount, phone, selectDate) {
    if (typeof mount === "string") mount = $(mount);
    if (!mount) return;
    var st = monthOf(phone);
    var year = st.year, month = st.month;
    var monthStr = year + "-" + pad(month);
    var selected = selectDate || mount.getAttribute("data-sc-selected") || "";
    // 当月且没有任何选中日：默认选中今天。提交后只看当天结果的用户进页即可见结果。
    // 选中落地必须走下面的 selectDate 路径（render 异步，渲染前查不到格子）。
    var autoToday = false;
    if (!selected) {
      var now = new Date();
      if (year === now.getFullYear() && month === now.getMonth() + 1) {
        selected = todayStr();
        autoToday = true;
      }
    }
    mount.setAttribute("data-sc-phone", phone);
    var grid = shell(mount);
    var label = mount.querySelector(".sc-month");
    // 上次失败留下的错误块换成骨架格（重试/切月同路径），别让错误文案陪加载转圈
    if (!grid.innerHTML || grid.querySelector(".sc-error")) grid.innerHTML = skeletonHtml();
    grid.setAttribute("aria-busy", "true");
    var t0 = performance.now();
    YB.api("GET", "/api/my-calendar?month=" + monthStr).then(function (data) {
      flags.sunday = !!data.sunday_sign;          // 管理员开启后周日照常显示/可查
      flags.saturday = data.saturday_sign === 1;  // 默认关闭（v0.29.0 起），开启后周六照常
      // 旧格保留到数据到达：慢请求先整体淡出，等过渡跑完再换内容并淡入（时长见 YB.SWAP_MS）；
      // 快请求直接替换。绝不能同帧/单帧移除 is-swapping —— 那会取消退出过渡，内容在
      // opacity 只掉到约 0.4–0.7 时就被拉回，看起来只是闪一下。
      var slow = performance.now() - t0 > ANIM_MIN_MS;
      var apply = function () {
        // 默认选中今天是"无人操作时"的兜底：若请求期间用户已点选，尊重用户选择，
        // 用已落库的 data-sc-selected 重绘选中态（否则会把用户的日期改回今天）。
        var picked = mount.getAttribute("data-sc-selected");
        if (autoToday && picked) { selected = picked; autoToday = false; }
        if (label) label.textContent = monthLabel(year, month);
        grid.innerHTML = gridHtml(data, phone, year, month, monthStr, selected);
        grid.removeAttribute("aria-busy");
        if (selectDate) {
          mount.setAttribute("data-sc-selected", selectDate);
          loadLog(mount, selectDate);
        } else if (autoToday) {
          mount.setAttribute("data-sc-selected", selected);
          // 日志面板全页共享：多账号同时自动选中今天时只发一次请求
          if (autoLoadedDate !== selected) {
            autoLoadedDate = selected;
            loadLog(mount, selected);
          }
        }
      };
      // 方向 = 本次月份 相对 上一次已渲染月份 的差（+1 往后、-1 往前、0 未换月）。
      // 月份状态在数据落地后才写回，故连续快点两次也按同一方向累计。
      var prevView = mount.getAttribute("data-sc-view");
      var dir = 0;
      if (prevView) {
        var p = prevView.split("-");
        dir = Math.sign((year * 12 + month) - (Number(p[0]) * 12 + Number(p[1])));
      }
      var commit = function () { mount.setAttribute("data-sc-view", year + "-" + pad(month)); };
      if (dir) {
        shiftMonth(grid, label, dir, function () { apply(); commit(); });
      } else {
        clearShift(grid); clearShift(label);
        if (slow) YB.swapOut([grid, label], function () { apply(); commit(); });
        else { apply(); commit(); }
      }
    }).catch(function (err) {
      // 卡内错误态 + 重试（P61-B5）：只换格内容，工具栏仍在（切月/今天也隐式重试）。
      // 文案用 YB.api 的友好消息（429/500/断网各有固定中文），不带堆栈与内部细节。
      var reason = (err && err.message) ? String(err.message) : "请稍后重试";
      clearShift(grid);
      clearShift(label);
      grid.innerHTML = '<p class="sc-error"><span>日历加载失败：' + esc(reason) + "</span>"
        + '<button type="button" class="btn btn--ghost btn--sm" data-sc-retry>重试</button></p>';
      grid.removeAttribute("aria-busy");
    });
  }

  /* ---------------- 日志面板 ---------------- */
  function logDate(text) {
    var el = document.querySelector("[data-sc-log-date]");
    if (el) el.textContent = text;
  }
  function logBody() { return document.querySelector("[data-sc-log]"); }

  // 空态结构（与 pages/user_calendar.html 的服务端静态块一致）。
  // note 只在"首次进入、还没选日期"时给：按日期的空结果再加一句说明是冗余。
  function emptyHtml(text, note) {
    return '<p class="sc-empty"><span class="sc-empty-icon" aria-hidden="true">'
      + svg("calendar") + "</span><span>" + text + "</span>"
      + (note ? '<span class="sc-empty-note">' + note + "</span>" : "") + "</p>";
  }

  function showLogPlaceholder() {
    var box = logBody();
    if (box) box.innerHTML = emptyHtml("点击日历中的日期，查看当天签到记录", "签到结果在每天调度后写入");
    logDate("选择日期查看当天记录");
  }

  // 写入日志面板内容；animate=true 时做一次淡入（仅慢请求调用）
  function setLogContent(box, html, animate) {
    box.innerHTML = html;
    if (!animate) return;
    // 关键帧是 160ms（.sc-log.is-fresh > *），一旦移除 is-fresh 即移除 animation →
    // 动画被截断（运气好只闪一帧）。故等 animationend（或 200ms 兜底）后再移除类。
    box.classList.add("is-fresh");
    var done = false;
    function finish() {
      if (done) return;
      done = true;
      box.removeEventListener("animationend", finish);
      box.classList.remove("is-fresh");
    }
    box.addEventListener("animationend", finish);
    setTimeout(finish, 200);
  }

  function loadLog(mount, date) {
    if (typeof mount === "string") mount = $(mount);
    var box = logBody();
    if (!box) return;
    var wd = new Date(date + "T00:00:00").getDay();
    logDate(date);
    // 周六/周日无需签到（各自开关关闭时）直接提示，不查日志
    if ((wd === 0 && !flags.sunday) || (wd === 6 && !flags.saturday)) {
      setLogContent(box, emptyHtml((wd === 0 ? "周日" : "周六") + "无需签到"));
      return;
    }
    // 取数期间**不替换内容**（否则"加载中 → 结果"会让面板高度与位置跳一下）：
    // 只把当前内容降透明度表示"正在取"，数据到达后整块换掉并淡入。
    box.classList.add("is-loading");
    var t0 = performance.now();
    YB.api("GET", "/api/my-logs?date=" + date).then(function (data) {
      var html = (!data.logs || !data.logs.length)
        ? emptyHtml(esc(date) + " 暂无签到记录")
        : '<pre class="log-view sc-log-text">' + esc(data.logs.join(String.fromCharCode(10))) + "</pre>";
      box.classList.remove("is-loading");
      setLogContent(box, html, performance.now() - t0 > ANIM_MIN_MS);
    }).catch(function (err) {
      box.classList.remove("is-loading");
      setLogContent(box, emptyHtml("读取失败，请稍后重试"));
      YB.toast.error(err && err.message ? err.message : "日志加载失败");
    });
  }

  // 堆叠布局（窄屏）下日志面板位于日历下方、可能在视口外：选中日期后就地把它带进视野，
  // 否则"点了日期却什么都没发生"（变化盲）。面板已在视野内时不动，避免无谓滚动。
  function revealLog() {
    var box = logBody();
    if (!box) return;
    var card = box.closest(".sc-log-card") || box;
    var r = card.getBoundingClientRect();
    var vh = window.innerHeight || document.documentElement.clientHeight;
    if (r.top < vh * 0.9 && r.bottom > 0) return;   // 已经看得到
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    try { card.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" }); }
    catch (e) { card.scrollIntoView(); }
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
    revealLog();
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
    if (t.closest("[data-sc-retry]")) {
      render(host, phone);   // 错误态重试：按当前月份重拉 /api/my-calendar
      return;
    }
    if (t.closest("[data-sc-shift]")) {
      var delta = Number(t.closest("[data-sc-shift]").getAttribute("data-sc-shift"));
      var st = monthOf(phone);
      st.month += delta;
      if (st.month < 1) { st.month = 12; st.year--; }
      if (st.month > 12) { st.month = 1; st.year++; }
      // 选中态由 data-sc-selected 带给下一次渲染，不再同步查 DOM（render 是异步的）
      render(host, phone);
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
  window.renderCalendar = render;   // 兼容旧调用名（pages/my_account.js 等）
  // 页面里没有日志面板时不显示占位（由页面模板自带空态）
  document.addEventListener("DOMContentLoaded", function () {
    if (logBody() && !logBody().querySelector(".sc-empty, .log-view")) showLogPlaceholder();
  });
})();
