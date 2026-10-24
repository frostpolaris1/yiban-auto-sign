// 外壳外观与交互层（core.js 拆壳，两刀）。
//   · 第一刀：主题、抽屉、服务器时钟、慢请求交换、浏览器级显示偏好；
//   · 第二刀（2026-10-24）：下拉菜单、导航分组（rail 浮层）、回到顶部、顶部导航进度条。
//
// 归属：全站唯一一份。layout_admin / layout_user / layout_auth 三个外壳模板共用。
// 载入顺序：**必须排在 core.js 之前**（见 tests/test_shell_chrome_shard.py 的顺序契约守卫）。
//
// 为什么拆这些（退役账「core.js 拆壳」）：core.js 曾是单文件 1,737 行全局脚本（ref=底本
// 5cbe2564）。本文件收的都是**叶子能力**——core.js 的请求层、缓存、模态栈、口令与门禁、
// 身份、导航徽标、分区 tab 都不引用它们，只有「初始化」与「事件委托」两处按名调用。
// 拆走后行为零变化，且每刀可独立回退（把该段搬回 core.js、删掉模板里的 script 标签即可）。
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
    // 72px 图标栏只能放下时分秒：把同一份时间以 data-clock-time 暴露给 CSS，
    // 由窄屏样式用 ::after 显示；完整日期+时间仍留在 [data-clock-text] 文本里，
    // 读屏与宽屏侧栏口径不变。
    var time = s.slice(11);
    T().forEach(document.querySelectorAll("[data-clock-text]"), function (n) {
      n.setAttribute("data-clock-time", time);
    });
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

  /* ---------- 下拉菜单（顶栏铃铛 / 头像 / 主题） ----------
     拆壳第二刀：本族与核心请求层、模态栈零耦合，只依赖基础工具与 DOM，
     故整体搬入外观层。core.js 的全局事件委托按名调用 X.*。
     焦点口径：菜单关闭必须把焦点还给触发器（否则键盘用户要重新 Tab 一遍）；
     打开即把焦点移入首个菜单项（WAI-ARIA menu-button）；零菜单项的面板
     （通知中心）把面板本身设为可聚焦容器，Esc 才有可归还的焦点。 */
  function closeDropdowns(except) {
    T().forEach(document.querySelectorAll(".dd-wrap.is-open"), function (w) {
      if (w === except) return;
      var a = document.activeElement;
      var hadFocus = w.contains(a);
      w.classList.remove("is-open");
      if (hadFocus) {
        var t = w.querySelector("[data-dropdown]");
        if (t && document.contains(t) && typeof t.focus === "function") t.focus();
      }
    });
  }
  function focusItem(items, index) {
    if (!items.length) return;
    items[((index % items.length) + items.length) % items.length].focus();
  }
  // 顶栏下拉的视口钳制。vendor 在 ≤720 把 .dd-menu 定成 width:calc(100vw - 16px) 且 right:-8px,
  // 面板右缘锚在**触发器右缘**；通知铃铛不是顶栏最后一个元素，锚点离视口右缘还有一段距离，
  // 面板就从左侧越界（实测 390 下 left=-50、320 下 left=-64）。纯 CSS 改不了：面板宽度由
  // 锚点右缘反推，锚点位置随顶栏排布（铃铛/主题/头像）变化，`max-width` 也管不到左溢出。
  // 故按「目标视口位置 − 锚点」一次算准，只保留 left 一种锚定：
  //   候选左缘 = 锚点右缘 − 面板宽，再夹进 [PAD, vw − PAD − 面板宽]。
  var DD_PAD = 8;
  function clampDropdown(wrap) {
    var menu = wrap.querySelector(".dd-menu");
    if (!menu) return;
    menu.style.left = "";
    menu.style.right = "";
    var vw = document.documentElement.clientWidth || window.innerWidth || 0;
    // 闭态带 scale(0.98)，矩形会缩水，量 offsetWidth（不受 transform 影响）
    var w = menu.offsetWidth;
    if (!w || !vw) return;
    var rightOff = parseFloat(window.getComputedStyle(menu).right);
    if (!isFinite(rightOff)) rightOff = 0;
    var wrapRect = wrap.getBoundingClientRect();
    var anchorRight = wrapRect.right - rightOff;
    var left = Math.min(Math.max(anchorRight - w, DD_PAD), Math.max(DD_PAD, vw - DD_PAD - w));
    menu.style.left = Math.round(left - wrapRect.left) + "px";
    menu.style.right = "auto";
  }
  function clampOpenDropdowns() {
    T().forEach(document.querySelectorAll(".dd-wrap.is-open"), clampDropdown);
  }
  function toggleDropdown(trigger) {
    var wrap = trigger && trigger.closest ? trigger.closest(".dd-wrap") : null;
    if (!wrap) return;
    var willOpen = !wrap.classList.contains("is-open");
    closeDropdowns(wrap);
    wrap.classList.toggle("is-open", willOpen);
    if (!willOpen) {
      var a = document.activeElement;
      if (wrap.contains(a)) {
        var trg = wrap.querySelector("[data-dropdown]");
        if (trg && typeof trg.focus === "function") trg.focus();
      }
      return;
    }
    clampDropdown(wrap);
    var menu = wrap.querySelector(".dd-menu");
    if (!menu) return;
    var item = menu.querySelector(".dd-menu-item");
    if (item) { item.focus(); return; }
    if (!menu.hasAttribute("tabindex")) menu.setAttribute("tabindex", "-1");
    menu.focus();
  }

  /* ---------- 导航分组（桌面手风琴 + 721–1100px rail 浮层定位） ---------- */
  function isRailMode() { return window.innerWidth > 720 && window.innerWidth <= 1100; }
  function positionRailFlyout(sub, trigger) {
    var r = trigger.getBoundingClientRect();
    sub.style.top = Math.max(8, Math.min(r.top, window.innerHeight - 60)) + "px";
  }
  function relayoutRail() {
    var groups = document.querySelectorAll("[data-nav-group].is-open");
    if (!isRailMode()) {
      T().forEach(groups, function (g) { var s = g.querySelector(".nav-submenu"); if (s) s.style.top = ""; });
      return;
    }
    T().forEach(groups, function (g) {
      var s = g.querySelector(".nav-submenu"), t = g.querySelector("[data-nav-toggle]");
      if (s && t) positionRailFlyout(s, t);
    });
  }

  /* ---------- 回到顶部（共享） ----------
     列表页的表格没有内滚上限、长列表整页滚动，因此需要一步回顶的入口。
     按钮固定在右下、滚动超过阈值才出现；只动 opacity/transform（无布局动画），
     reduced-motion 去位移、点击直接回顶（不做平滑滚动）。键盘可达（原生 button）。 */
  var TO_TOP_AT = 400;
  function initBackToTop() {
    if (document.getElementById("to-top")) return;
    var btn = T().el("button", { type: "button", id: "to-top", class: "to-top", "aria-label": "回到顶部", title: "回到顶部" });
    btn.appendChild(T().el("span", { class: "to-top__icon", "aria-hidden": "true", html: T().svgUse("arrow-up") }));
    (document.body || document.documentElement).appendChild(btn);
    function sync() {
      var y = window.pageYOffset || document.documentElement.scrollTop || 0;
      btn.classList.toggle("is-show", y > TO_TOP_AT);
    }
    btn.addEventListener("click", function () {
      if (T().reducedMotion()) window.scrollTo(0, 0);
      else window.scrollTo({ top: 0, behavior: "smooth" });
      btn.blur();   // 回顶后按钮隐去，焦点归还文档主体，避免焦点停在不可见节点
    });
    window.addEventListener("scroll", sync, { passive: true });
    sync();
  }

  /* ---------- 顶部导航进度条（MPA 页面切换） ----------
     多页应用没有前端路由，点内部链接即整页跳转；浏览器自身不提供"正在导航"反馈，
     弱网下会出现"点了一下没反应 → 突然白屏换页"的跳变感。本模块在捕获阶段监听
     合格的同源导航点击，立即显示细进度条并缓慢推进；新页面载入时读 sessionStorage
     里的起点时间，接续补到 100% 再淡出。不合格的链接一律放行：修饰键（新标签/
     下载）、target!=_self、download、外链、仅 hash 同页、文件类扩展名、
     显式 data-no-nav-progress。reduced-motion 直接不显示。
     明确不做「预取」：页面响应是 Cache-Control: no-store（见 app.py
     _NO_STORE_PAGES），prefetch 拉到的整页无法在导航时复用，只会在单 worker 上
     白跑一遍渲染；各页共享的 JS/CSS 本身已带版本号缓存。 */
  var NAV_PROGRESS_KEY = "yiban-nav-progress-at";
  var navBar = null, navTimer = null, navValue = 0, navGuardTimer = null;
  // 兜底收尾：进度条靠**新页面**补到 100%；若那次点击最终没发生导航（被页面逻辑
  // 拦下、或浏览器取消），条会永远停在 90%。兜底计时器保证任何情况下都自行收尾。
  var NAV_GUARD_MS = 8000;
  var NAV_FILE_RE = /\.(png|jpe?g|gif|svg|webp|ico|pdf|zip|gz|log|csv|xlsx?|docx?|pptx?|mp4|mp3|txt|json)$/i;
  function isNavLink(a) {
    if (!a || !a.getAttribute) return false;
    if (a.hasAttribute("download") || a.hasAttribute("data-no-nav-progress")) return false;
    var target = String(a.getAttribute("target") || "").toLowerCase();
    if (target && target !== "_self") return false;
    var href = a.getAttribute("href");
    if (!href || href.charAt(0) === "#") return false;
    if (/^(mailto|tel|javascript):/i.test(href)) return false;
    var u;
    try { u = new URL(a.href, location.href); } catch (e) { return false; }
    if (u.origin !== location.origin) return false;                       // 外链
    if (u.pathname === location.pathname && u.search === location.search) return false;  // 同页（含仅 hash）
    if (NAV_FILE_RE.test(u.pathname)) return false;                       // 下载类资源
    return true;
  }
  function navBarCreate() {
    if (navBar && document.body && document.body.contains(navBar)) return navBar;
    navBar = T().el("div", { class: "nav-progress", "aria-hidden": "true" });
    navBar.appendChild(T().el("div", { class: "nav-progress__fill" }));
    (document.body || document.documentElement).appendChild(navBar);
    return navBar;
  }
  function navSetWidth(pct) { if (navBar) navBar.firstChild.style.width = pct + "%"; }
  function startNavProgress() {
    if (T().reducedMotion()) return;
    navBarCreate();
    navBar.classList.remove("is-done");
    navBar.firstChild.style.opacity = "1";
    navValue = 8;
    navSetWidth(navValue);
    try { sessionStorage.setItem(NAV_PROGRESS_KEY, String(Date.now())); } catch (e) {}
    clearInterval(navTimer);
    // 越接近 90% 推进越慢：避免还没到达新页就先满格（满格后长时间不动反而"卡住"）
    navTimer = setInterval(function () {
      navValue += Math.max(0.4, (90 - navValue) * 0.08);
      if (navValue >= 90) { navValue = 90; clearInterval(navTimer); }
      navSetWidth(navValue);
    }, 120);
    clearTimeout(navGuardTimer);
    navGuardTimer = setTimeout(function () {
      if (document.visibilityState !== "hidden") finishNavProgress();
    }, NAV_GUARD_MS);
  }
  function finishNavProgress() {
    clearTimeout(navGuardTimer);
    if (!navBar || T().reducedMotion()) return;
    clearInterval(navTimer);
    navSetWidth(100);
    navBar.classList.add("is-done");
    var node = navBar;
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
      if (navBar === node) { navBar = null; navValue = 0; }
    }, 240);
  }
  function initNavProgress() {
    // 新页载入接续：上一页点过导航（15s 内）则从 85% 补到 100% 并淡出，收尾"已到达"
    var startedAt = 0;
    try {
      startedAt = parseInt(sessionStorage.getItem(NAV_PROGRESS_KEY) || "0", 10) || 0;
      sessionStorage.removeItem(NAV_PROGRESS_KEY);
    } catch (e) {}
    if (!T().reducedMotion() && startedAt && Date.now() - startedAt < 15000) {
      navBarCreate();
      navValue = 85;
      navSetWidth(85);
      requestAnimationFrame(function () { requestAnimationFrame(finishNavProgress); });
    }
    document.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var t = e.target;
      var a = (t && t.closest) ? t.closest("a[href]") : null;
      if (isNavLink(a)) startNavProgress();
    }, true);
    // 真正发生导航时取消兜底：条的去向由新页面负责；往返缓存恢复时清掉残留。
    window.addEventListener("pagehide", finishNavProgress);
    window.addEventListener("pageshow", function (e) { if (e.persisted) finishNavProgress(); });
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
  // 拆壳第二刀：下拉 / 导航分组 / 回顶 / 导航进度条
  X.closeDropdowns = closeDropdowns;
  X.focusItem = focusItem;
  X.clampDropdown = clampDropdown;
  X.clampOpenDropdowns = clampOpenDropdowns;
  X.toggleDropdown = toggleDropdown;
  X.isRailMode = isRailMode;
  X.positionRailFlyout = positionRailFlyout;
  X.relayoutRail = relayoutRail;
  X.initBackToTop = initBackToTop;
  X.initNavProgress = initNavProgress;
})();
