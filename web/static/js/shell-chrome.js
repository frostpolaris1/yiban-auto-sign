// 外壳外观层（core.js 拆壳第一刀）：主题、抽屉、服务器时钟、慢请求交换、浏览器级显示偏好。
//
// 归属：全站唯一一份。layout_admin / layout_user / layout_auth 三个外壳模板共用。
// 载入顺序：**必须排在 core.js 之前**（见 tests/test_web_js_modules.py 的顺序契约守卫）。
//
// 为什么拆这一刀（退役账「core.js 拆壳」）：core.js 曾是单文件 ~1,800 行全局脚本。
// 本文件收的都是**叶子能力**——core.js 的请求层、模态栈、身份、导航徽标、分区 tab
// 都不引用它们，只有「初始化」与「事件委托」两处按名调用。拆走后行为零变化，
// 且本刀可独立回退（把这段搬回 core.js、删掉模板里的 script 标签即可）。
//
// 分片间通信（**这是本刀唯一的设计约束**）：本文件先于 core.js 执行，故
//   · 取 core.js 的基础工具（el / svgUse / reducedMotion / setText / forEach / api /
//     apiCached）必须**在调用时**读 `window.YB_INTERNALS`——定义期读不到（core.js 还没跑）；
//   · 本层能力挂到 `window.YB.__chrome`，由 core.js 在载入时捕获并转挂到公开面 `YB.*`
//     （公开面不变：shell.ts / 页面脚本仍走 `YB.toggleTheme` / `YB.getServerNow` 等）。
// 为什么不反过来（core.js 先载、本文件后载）：defer 脚本执行时 `document.readyState`
//   已是 "interactive"，core.js 的 onReady 会**立即**跑初始化，那一刻后载的分片还不存在。
(function () {
  "use strict";
  var YB = window.YB = window.YB || {};
  var X = YB.__chrome = {};
  // core.js 的基础工具：调用时读，见文件头"分片间通信"。
  function T() { return window.YB_INTERNALS || {}; }

  /* ---------- 主题 ---------- */
  function currentTheme() { return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light"; }
  function updateThemeIcons(theme) {
    var name = (theme || currentTheme()) === "dark" ? "sun" : "moon";
    T().forEach(document.querySelectorAll("#themeToggle, [data-theme-btn]"), function (btn) { btn.innerHTML = T().svgUse(name); });
  }
  function applyTheme(theme, persist) {
    theme = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme);
    if (persist !== false) { try { localStorage.setItem("yiban-theme", theme); } catch (e) {} }
    updateThemeIcons(theme);
    try { document.dispatchEvent(new CustomEvent("yiban:theme", { detail: { theme: theme } })); } catch (e) {}
  }
  function toggleTheme() { applyTheme(currentTheme() === "dark" ? "light" : "dark"); }

  /* ---------- 抽屉 ---------- */
  function toggleDrawer(open) {
    if (open === undefined) open = !document.body.classList.contains("has-drawer-open");
    document.body.classList.toggle("has-drawer-open", !!open);
  }

  /* ---------- 服务器时钟 ---------- */
  var clock = { offset: 0, tz: 0, status: "", color: "", ready: false };
  // 浏览器相对 UTC 的分钟偏移（东八区 = +480）。
  function browserTzMin() { return -new Date().getTimezoneOffset(); }
  // 服务器墙上时钟：由本地 getter（getFullYear/getMonth/getDate/getHours…）读出的值必须
  // 等于服务器当地时间。Date.now() 是 UTC 时刻，加 offset 得到服务器真实时刻；再叠加
  // 「服务器时区 − 浏览器时区」才能让本地 getter 落在服务器墙上时间上。
  // 旧实现只加服务器时区：UTC+8 浏览器会再叠一次 +8h，16:00 后 getDate() 直接跳到次日，
  // 造成数据总览「今日」KPI 与热力图取不到当天键（两侧取日都必须用同一个时刻源）。
  // 未校准前退回浏览器本地时钟，避免把未加时区的 UTC 当成服务器墙上时间。
  function serverNow() {
    if (!clock.ready) return new Date();
    var epoch = Math.floor(Date.now() / 1000) + clock.offset + (clock.tz - browserTzMin()) * 60;
    return new Date(epoch * 1000);
  }
  function two(n) { return (n < 10 ? "0" : "") + n; }
  // 必须用本地 getter 重建：serverNow() 的本地字段即服务器墙上时间；若再走 toISOString
  //（UTC 表示）会把服务器时区重复扣一次。格式与旧实现同为 "YYYY-MM-DD HH:MM:SS"。
  function clockString() {
    var d = serverNow();
    return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate()) + " " +
      two(d.getHours()) + ":" + two(d.getMinutes()) + ":" + two(d.getSeconds());
  }
  function renderClock() {
    var s = clockString();
    T().setText("[data-clock-text]", s);
    T().setText("[data-clock-now]", s);
  }
  function reflectSignStatus() {
    if (!clock.status) return;
    T().forEach(document.querySelectorAll("[data-sign-status]"), function (n) {
      n.textContent = clock.status;
      if (clock.color) n.style.color = clock.color;
    });
  }
  function clockInfo() {
    // server_ts 用真实服务器 epoch（Date.now()+offset），不是上面那个已按墙上时间平移过的时刻
    return { now: clockString(), server_ts: Math.floor(Date.now() / 1000) + clock.offset, tz_offset_min: clock.tz, sign_status: clock.status, color: clock.color };
  }
  // 时钟：外壳每次加载校准一次即可（offset 不随时间衰减），用 10s 短 TTL 缓存——
  // 快速切页（<10s）不再每页都拉 /api/clock；页面停留期间由 30s 定时器 force 拉取
  // 刷新 sign_status/颜色。需要更实时状态的页面语义不受影响（各自定时刷新）。
  function calibrateClock(opts) {
    var force = !!(opts && opts.force);
    var doFetch = function () { return T().api("GET", "/api/clock"); };
    return (force ? doFetch() : T().apiCached("clock", 10000, doFetch)).then(function (data) {
      if (!data) return null;
      var ts = Number(data.server_ts);
      if (isFinite(ts)) clock.offset = ts - Math.floor(Date.now() / 1000);
      clock.tz = Number(data.tz_offset_min) || 0;
      clock.status = data.sign_status || "";
      clock.color = /^#[0-9a-f]{6}$/i.test(String(data.color || "")) ? data.color : "";
      clock.ready = true;   // 校准完成：serverNow() 起按服务器墙上时间解释本地字段
      renderClock(); reflectSignStatus();
      try { document.dispatchEvent(new CustomEvent("yiban:clock", { detail: clockInfo() })); } catch (e) {}
      return data;
    }).catch(function () { return null; });
  }

  /* ---------- 慢请求的「整体淡出 → 换内容 → 淡入」 ----------
     统一口径：加 is-swapping → 等退出过渡跑完（主元素的 transitionend 或 SWAP_MS
     兜底，先到者）→ 执行 render 回调 → 再移除 is-swapping 走进入过渡。
     为什么不能同帧/单帧移除：移除类会立刻取消 opacity 过渡，内容在 opacity 只掉到
     0.4–0.7 时就被拉回，看起来只是闪一下。主元素取数组首项（其 160ms 过渡即交换窗口），
     其余元素只同步切换类名，避免较短的过渡（如月份标题 120ms）提前打断主元素淡出。 */
  var SWAP_MS = 160;
  function swapOut(nodes, cb) {
    var els = [].concat(nodes || []).filter(Boolean);
    if (!els.length) { cb(); return; }
    var primary = els[0];
    var done = false;
    function finish() {
      if (done) return;
      done = true;
      primary.removeEventListener("transitionend", onEnd);
      if (cb) cb();
      els.forEach(function (n) { n.classList.remove("is-swapping"); });
    }
    function onEnd(e) { if (e.target === primary) finish(); }
    primary.addEventListener("transitionend", onEnd);
    els.forEach(function (n) { n.classList.add("is-swapping"); });
    setTimeout(finish, SWAP_MS);
  }

  /* ---------- 浏览器级显示偏好（localStorage） ----------
     仅影响本机显示密度，不涉及任何后端策略，故与 yiban-theme 同层使用 localStorage。
     归属邮箱开关：账号表窄屏在名称单元格内补一行归属邮箱，由本偏好控制显隐；
     默认开（键缺失=开），关闭后宽屏归属列不受影响。取值点集中在账号管理页的
     frontend/src/accounts/Accounts.vue 一处（经 YB.prefs.ownerEmailVisible 桥接），
     改后下次渲染即生效（无需后端往返）。 */
  var PREF_OWNER_EMAIL = "yiban-owner-email";
  function ownerEmailVisible() {
    try { return localStorage.getItem(PREF_OWNER_EMAIL) !== "0"; } catch (e) { return true; }
  }
  function setOwnerEmailVisible(v) {
    try { localStorage.setItem(PREF_OWNER_EMAIL, v ? "1" : "0"); } catch (e) {}
    try { document.dispatchEvent(new CustomEvent("yiban:owner-email-pref", { detail: { visible: !!v } })); } catch (e) {}
  }

  /* ---------- 本层出口（core.js 捕获后转挂到公开面 YB.*） ---------- */
  X.currentTheme = currentTheme;
  X.updateThemeIcons = updateThemeIcons;
  X.applyTheme = applyTheme;
  X.toggleTheme = toggleTheme;
  X.toggleDrawer = toggleDrawer;
  X.calibrateClock = calibrateClock;
  X.renderClock = renderClock;
  X.serverNow = serverNow;
  X.clockString = clockString;
  X.clockInfo = clockInfo;
  X.swapOut = swapOut;
  X.SWAP_MS = SWAP_MS;
  X.ownerEmailVisible = ownerEmailVisible;
  X.setOwnerEmailVisible = setOwnerEmailVisible;
})();
