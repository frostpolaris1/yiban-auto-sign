// 全局外壳交互层（Adminator 4.3.0 外壳）：请求层（CSRF / 401 重试 / 超时中止 / 并发 GET 去重 / 外壳数据缓存）、
// toast、模态、口令与倒计时门禁、时钟、身份、导航徽标、主题、tab 深链。classic script（非 module）。
// 归属与复用：全站唯一一份外壳行为层，layout_admin / layout_user / layout_auth 三个外壳模板共用；页面与组件脚本一律
//   经 window.YB.*（或本文件末尾为内联 onclick 保留的裸全局出口）复用，不再各自实现请求与弹窗。
// 通信（入）：partials/theme_boot.html 先定义全局 BASE 作路径前缀；本文件必须排在 pages/*.js 之前载入（顺序契约见
//   tests/test_web_js_modules.py）。通信（出）：外壳只读端点 GET /api/me、/api/clock、/api/announcement、
//   /api/accounts、/api/users、/api/changelog 与会话端点 POST /api/logout，实现见 web/routes/ 下各 blueprint。
(function () {
  "use strict";
  if (window.YB && window.YB.__ready) return; // 外壳与页面可能各引一次

  var APP_BASE = (typeof BASE === "string") ? BASE : "";

  /* ---------- 基础工具 ---------- */
  function forEach(list, fn) { Array.prototype.forEach.call(list || [], fn); }
  function $(id) { return document.getElementById(id); }
  function url(path) { return (/^[a-z][a-z0-9+.-]*:/i.test(path) || path.charAt(0) !== "/") ? path : APP_BASE + path; }
  function escapeHtml(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function svgUse(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  // SVG 命名空间下的图标元素构造：<svg>/<use> 必须以 createElementNS 创建，否则浏览器
  // 视为未知 HTML 元素、图标不渲染；页面脚本据此以 DOM 方式挂图标，替代 innerHTML 常量串。
  function iconEl(name, cls) {
    var NS = "http://www.w3.org/2000/svg";
    var s = document.createElementNS(NS, "svg");
    s.setAttribute("aria-hidden", "true");
    if (cls) s.setAttribute("class", cls);
    var u = document.createElementNS(NS, "use");
    u.setAttribute("href", "#i-" + name);
    s.appendChild(u);
    return s;
  }
  // 手机号展示层脱敏（幂等）：已含 * 原样返回；长度 >=7 保留前 3 后 4。
  // 各页面统一走本助手，避免脱敏口径在页面脚本里各写一份。
  function maskPhone(p) {
    p = String(p || "");
    if (p.indexOf("*") !== -1) return p;
    return p.length >= 7 ? p.slice(0, 3) + "****" + p.slice(-4) : p;
  }
  // 邮箱展示层脱敏（幂等，与后端 _mask_email 同口径）：保留最多 3 个字符 + 域名；
  // 已含 * 或非邮箱（无 @ / @ 在首位）原样返回。完整邮箱只允许存在于 JS 内存态与
  // 请求体/URL path，禁止写入 DOM 文本或属性（用户管理页据此渲染，见 pages/work_users.js）。
  function maskEmail(e) {
    e = String(e == null ? "" : e);
    if (e.indexOf("*") !== -1) return e;
    var i = e.indexOf("@");
    if (i <= 0) return e;
    return e.slice(0, Math.min(3, i)) + "***" + e.slice(i);
  }
  // 常量 SVG 片段走 html；动态文本一律走 text，避免把不可信数据交给 innerHTML
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) forEach(Object.keys(attrs), function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === "class" || k === "className") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k === "dataset") forEach(Object.keys(v), function (d) { node.dataset[d] = v[d]; });
      else if (k === "style" && typeof v === "object") forEach(Object.keys(v), function (s) { node.style[s] = v[s]; });
      else if (k === "for") node.htmlFor = v;
      else if (k.indexOf("on") === 0 && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
      else node.setAttribute(k, v === true ? "" : v);
    });
    if (children != null) forEach([].concat(children), function (c) {
      if (c == null) return;
      node.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
    });
    return node;
  }
  function setText(selector, value) { forEach(document.querySelectorAll(selector), function (n) { n.textContent = value; }); }
  function onReady(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }
  function reducedMotion() { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }

  /* ---------- 请求层：CSRF / 401 重试 / 非 JSON 兜底 ---------- */
  var csrfToken = "";
  var mePromise = null;
  function httpError(status, message, data) {
    var e = new Error(message || ("请求失败 (" + status + ")"));
    e.status = status; e.error = e.message; e.data = data; e.isHttp = true; return e;
  }
  function networkError(cause) {
    var e = new Error("网络连接失败，请检查网络后重试");
    e.network = true; e.isNetwork = true; e.cause = cause; return e;
  }
  function timeoutError() {
    var e = new Error("请求超时，请检查网络后重试");
    e.network = true; e.isNetwork = true; e.timeout = true; return e;
  }
  // 请求超时上限：fetch 默认**没有超时**，网络静默掉线（手机切换网络、NAT 静默丢弃）或
  // 服务端线程占满时，Promise 会一直挂着 —— 页面上的骨架/加载条/在途禁用按钮就永不结束
  // 即"总览页一直转圈、永不完成"这一形态。给每个请求挂 AbortSignal，
  // 超时按网络错误处理：既有失败态与「重试」入口随即接管，不再出现"永远转圈"。
  var API_TIMEOUT_MS = 20000;
  function genericMessage(status) {
    return status === 413 ? "请求内容过大，已拒绝"
      : status === 400 ? "请求参数有误"
      : status === 429 ? "请求过于频繁，请稍后再试"
      : status >= 500 ? "服务器内部错误"
      : "请求失败 (" + status + ")";
  }
  function fetchMe() {
    if (!mePromise) {
      mePromise = fetch(url("/api/me"), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.text(); })
        .then(function (t) { try { return t ? JSON.parse(t) : {}; } catch (e) { return {}; } })
        .then(function (d) { if (d && d.csrf_token) csrfToken = d.csrf_token; return d || {}; })
        .catch(function () { return {}; })
        .then(function (d) { mePromise = null; return d; });
    }
    return mePromise;
  }
  function perform(req, retried) {
    var headers = { Accept: "application/json" };
    var body = req.body;
    if (body != null && typeof body !== "string") { headers["Content-Type"] = "application/json"; body = JSON.stringify(body); }
    else if (typeof body === "string" && body) { headers["Content-Type"] = "application/json"; }
    var write = req.method === "POST" || req.method === "PUT" || req.method === "DELETE" || req.method === "PATCH";
    if (write && csrfToken) headers["X-CSRF-Token"] = csrfToken;
    if (req.headers) forEach(Object.keys(req.headers), function (k) { headers[k] = req.headers[k]; });
    // 超时兜底：只会 abort 本次请求，失败按网络错误抛出（见 timeoutError）。
    // 写请求不加自动重试——重试语义仍由 handleResponse 的 401/403 分支独占。
    var timer = null, ctl = (typeof AbortController === "function") ? new AbortController() : null;
    var opts = { method: req.method, headers: headers, body: body, credentials: "same-origin" };
    if (ctl) opts.signal = ctl.signal;
    if (ctl) timer = setTimeout(function () { ctl.abort(); }, API_TIMEOUT_MS);
    var done = function () { if (timer) { clearTimeout(timer); timer = null; } };
    return fetch(url(req.path), opts)
      .then(function (resp) {
        return resp.text().then(function (txt) { return handleResponse(resp, txt, req, retried); });
      }, function (err) {
        // 超时中止与网络故障分开报文案：前者提示"超时"（同一动作值得重试），
        // 后者才是断网（检查网络）。两者都带 network 语义，页面失败态与重试入口一致。
        throw (err && err.name === "AbortError") ? timeoutError() : networkError(err);
      })
      // 写请求成功返回后整体失效外壳缓存（保守实现：写后重取，绝不把旧壳数据粘住；
      // 退出/登录这类会话边界本身也是写请求，缓存随之清空，不会串会话）。
      .then(function (data) { if (write) cacheClearAll(); done(); return data; }, function (err) { done(); throw err; });
  }
  function handleResponse(resp, txt, req, retried) {
    var data = null;
    try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = null; }
    if (data === null) {
      if (resp.status === 401 && !retried) return refreshThenRetry(req);
      throw httpError(resp.status, genericMessage(resp.status));
    }
    if (resp.status === 401 && !retried) return refreshThenRetry(req);
    if (resp.status === 403 && !retried && /校验失败|CSRF|令牌/.test(String(data.error || ""))) {
      // 刷新身份 → 重试一次。**只有"刷新身份"这一步失败**才回落到通用文案：
      // 重试请求自身被拒时必须原样上抛，否则后端真正的 403 文案会被吞掉
      // （实测：口令门那条「口令校验未通过，设置未生效」被换成了"请刷新页面后重试"，
      //  操作者看不出是口令错了；同一条路也吞 409/403 这类业务文案）。
      csrfToken = "";
      return fetchMe().catch(function () { throw httpError(403, "请刷新页面后重试"); })
        .then(function () { return perform(req, true); });
    }
    if (!resp.ok || data.ok === false) throw httpError(resp.status, data.error, data);
    return data;
  }
  function refreshThenRetry(req) {
    csrfToken = "";
    return fetchMe().then(function () { return perform(req, true); });
  }
  function normalizeRequest(method, path, body) {
    if (typeof method === "string" && method.charAt(0) === "/") { // 兼容 api(path, {method, body})
      var opts = (path && typeof path === "object") ? path : {};
      return { method: (opts.method || "GET").toUpperCase(), path: method, body: opts.body, headers: opts.headers };
    }
    return { method: (method || "GET").toUpperCase(), path: path, body: body };
  }
  /* 并发 GET 去重：同一时刻多个调用方请求同一 URL 时只发一次网络请求，共享同一 Promise。
     仅合并「尚未返回」的请求，一旦落地即从表中移除——不引入任何响应缓存，后续刷新或写操作
     后的重新拉取仍拿到最新数据，新鲜度语义不变；POST/PUT/DELETE/PATCH 一律不走此路径。
     动机：外壳 core.js 的导航徽标/时钟/公告与页面脚本会在首屏同时拉 /api/accounts、
     /api/users、/api/clock、/api/announcement，不去重的话每次切页都要把这些重问一遍（单 worker 生产
     环境下白占线程与带宽）。 */
  var inflightGets = {};
  function api(method, path, body) {
    var req = normalizeRequest(method, path, body);
    if (req.method !== "GET") return perform(req, false);
    var key = req.path + "\u0000" + (req.body == null ? "" : String(req.body))
      + "\u0000" + JSON.stringify(req.headers || {});
    if (inflightGets[key]) return inflightGets[key];
    var pending = perform(req, false);
    inflightGets[key] = pending;
    var clear = function () { if (inflightGets[key] === pending) delete inflightGets[key]; };
    pending.then(clear, clear);
    return pending;
  }

  /* ---------- 外壳数据客户端缓存（sessionStorage） ----------
     目标：外壳级低频数据一次加载后读缓存，需要实时的分区各自按 TTL 定时刷新。
     MPA 每个页面加载都重跑外壳初始化（/api/me、/api/announcement、导航徽标、时钟），
     快速切页时同一份外壳数据被反复拉取——既是单 worker 上的无谓请求，也是触发全局限速
     429 的主因。apiCached 按 key 缓存成功结果（带写入时间戳 + TTL），命中则不产生网络请求；
     任何写请求成功返回后整体失效（见 perform 的 cacheClearAll），会话边界（登录/退出）
     因此天然清理，不会串会话。失败/空结果不写缓存，避免把错误态粘住。
     sessionStorage 按标签页隔离，键前缀统一便于整体清理与排查。 */
  var CACHE_PREFIX = "yiban-cache:";
  function cacheGet(key) {
    try {
      var raw = sessionStorage.getItem(CACHE_PREFIX + key);
      if (!raw) return null;
      var rec = JSON.parse(raw);
      if (!rec || typeof rec.t !== "number" || typeof rec.ttl !== "number") return null;
      if (Date.now() - rec.t > rec.ttl) { sessionStorage.removeItem(CACHE_PREFIX + key); return null; }
      return rec.v;
    } catch (e) { return null; }
  }
  function cacheSet(key, ttlMs, value) {
    try {
      sessionStorage.setItem(CACHE_PREFIX + key, JSON.stringify({ t: Date.now(), ttl: ttlMs, v: value }));
    } catch (e) { /* 隐私模式/配额满：静默降级为不缓存 */ }
  }
  function cacheClearAll() {
    try {
      var keys = [];
      for (var i = 0; i < sessionStorage.length; i++) {
        var k = sessionStorage.key(i);
        if (k && k.indexOf(CACHE_PREFIX) === 0) keys.push(k);
      }
      forEach(keys, function (k) { sessionStorage.removeItem(k); });
    } catch (e) {}
  }
  function apiCached(key, ttlMs, fn) {
    var hit = cacheGet(key);
    if (hit !== null) return Promise.resolve(hit);
    return Promise.resolve().then(fn).then(function (v) {
      if (v !== undefined && v !== null) cacheSet(key, ttlMs, v);
      return v;
    });
  }

  /* ---------- Toast ---------- */
  var TOAST_ICON = { success: "circle-check", error: "circle-x", danger: "circle-x", warning: "triangle-alert", info: "info" };
  var toastNodes = [];
  function toastHost() {
    var h = $("toast-host");
    if (!h) { h = el("div", { id: "toast-host", class: "toast-host", "aria-live": "polite", "aria-atomic": "true" }); document.body.appendChild(h); }
    return h;
  }
  function dismissToast(rec) {
    if (!rec || rec.dead) return;
    rec.dead = true;
    clearTimeout(rec.timer);
    rec.node.classList.remove("is-shown");
    rec.node.classList.add("is-hiding");
    toastNodes = toastNodes.filter(function (t) { return t !== rec; });
    setTimeout(function () { if (rec.node.parentNode) rec.node.parentNode.removeChild(rec.node); }, 220);
  }
  function showToast(type, msg, opts) {
    type = TOAST_ICON[type] ? type : "info";
    msg = String(msg == null ? "" : msg);
    opts = opts || {};
    var duration = opts.duration || 3200;
    for (var i = 0; i < toastNodes.length; i++) { // 相同消息高频重复时延长现有提示
      var old = toastNodes[i];
      if (!old.dead && old.type === type && old.msg === msg && Date.now() - old.at < 1500) {
        clearTimeout(old.timer);
        old.timer = setTimeout(function () { dismissToast(old); }, duration);
        return old.node;
      }
    }
    var node = el("div", { class: "toast toast--" + type, role: "status" });
    var close = el("button", { type: "button", class: "toast__close", "aria-label": "关闭", html: svgUse("x") });
    var rec = { node: node, type: type, msg: msg, at: Date.now(), timer: null, dead: false };
    close.addEventListener("click", function () { dismissToast(rec); });
    node.appendChild(el("span", { class: "toast__icon", html: svgUse(TOAST_ICON[type]) }));
    node.appendChild(el("div", { class: "toast__msg", text: msg }));
    node.appendChild(close);
    toastHost().appendChild(node);
    toastNodes.push(rec);
    if (reducedMotion()) node.classList.add("is-shown");
    else requestAnimationFrame(function () { node.classList.add("is-shown"); });
    rec.timer = setTimeout(function () { dismissToast(rec); }, duration);
    return node;
  }
  function toast(msg, isError) { return showToast(isError ? "error" : "info", msg); }      // 出口有两种并存形态（直呼 toast(msg, isError) 与取变体 toast.success/.error/.info），改签名要同时顾及这二者的调用方
  toast.success = function (m, o) { return showToast("success", m, o); };
  toast.error = function (m, o) { return showToast("error", m, o); };
  toast.warning = function (m, o) { return showToast("warning", m, o); };
  toast.info = function (m, o) { return showToast("info", m, o); };
  toast.dismiss = dismissToast;

  /* ---------- 模态管理器（叠层 / Esc / Tab 圈闭 / 滚动锁 / 焦点归还） ---------- */
  var modalStack = [];
  var scrollLocks = 0;
  function lockScroll() {
    scrollLocks++;
    if (scrollLocks > 1) return;
    var gap = window.innerWidth - document.documentElement.clientWidth;
    if (gap > 0) document.body.style.paddingRight = gap + "px";
    document.documentElement.classList.add("pm-scroll-lock");
  }
  function unlockScroll() {
    if (scrollLocks > 0) scrollLocks--;
    if (scrollLocks > 0) return;
    document.documentElement.classList.remove("pm-scroll-lock");
    document.body.style.paddingRight = "";
  }
  function focusables(root) {
    var sel = 'button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])';
    return Array.prototype.filter.call(root.querySelectorAll(sel), function (x) { return !x.disabled && x.offsetParent !== null; });
  }
  function appendBody(container, body) {
    if (body == null) return;
    if (body.nodeType) container.appendChild(body);
    else if (typeof body === "string") container.innerHTML = body; // 调用方保证为可信/已转义标记
    else container.appendChild(document.createTextNode(String(body)));
  }
  var uidSeq = 0;
  function openModal(arg, trigger) {
    if (arg && arg.nodeType === 1) return adoptModal(arg, trigger);
    var cfg = arg || {};
    trigger = trigger || document.activeElement;
    var titleId = "pm-title-" + (++uidSeq);
    var panel = el("div", { class: "pm-panel" + (cfg.size === "lg" ? " pm-panel--lg" : ""), role: "dialog", "aria-modal": "true", tabindex: "-1" });
    if (cfg.labelledBy) panel.setAttribute("aria-labelledby", cfg.labelledBy);
    else if (cfg.title) panel.setAttribute("aria-labelledby", titleId);
    var head = el("div", { class: "modal-head" });
    var headText = el("div", { class: "modal-head-text" });
    var titleEl = el("div", { class: "modal-title", id: titleId, text: cfg.title || "" });
    headText.appendChild(titleEl);
    // 副标题（可选）：与标题同处头部、紧贴其下，而不是隔着整段正文内距
    if (cfg.subtitle) headText.appendChild(el("div", { class: "modal-sub", text: cfg.subtitle }));
    head.appendChild(headText);
    var handle = {
      el: panel, panel: panel, backdrop: null, trigger: trigger,
      dismissible: cfg.dismissible !== false, onClose: cfg.onClose,
      titleEl: titleEl,
      close: function () { closeModal(handle); },
      setTitle: function (t) { titleEl.textContent = t; },
      setSubtitle: function (t) {
        var sub = headText.querySelector(".modal-sub");
        if (sub) sub.textContent = t;
        else headText.appendChild(el("div", { class: "modal-sub", text: t }));
      },
      setBody: function (b) { body.innerHTML = ""; appendBody(body, b); }
    };
    panel.appendChild(head);
    var body = el("div", { class: "modal-body" });
    appendBody(body, cfg.body);
    panel.appendChild(body);
    if (cfg.actions && cfg.actions.length) {
      var foot = el("div", { class: "modal-foot" });
      forEach(cfg.actions, function (action) {
        var btn = el("button", { type: "button", class: "btn btn--" + (action.variant || "ghost"), text: action.label || "" });
        btn.addEventListener("click", function () {
          var result = action.onClick ? action.onClick(handle) : undefined;
          if (result !== false && action.close !== false) closeModal(handle);
        });
        foot.appendChild(btn);
      });
      panel.appendChild(foot);
    }
    if (cfg.dismissible !== false) {
      var closeBtn = el("button", { type: "button", class: "pm-panel-close", "aria-label": "关闭", html: svgUse("x") });
      closeBtn.addEventListener("click", function () { closeModal(handle); });
      head.appendChild(closeBtn);
    }
    var backdrop = el("div", { class: "pm-backdrop", hidden: true, "data-modal-backdrop": "" });
    if (modalStack.length) backdrop.classList.add("pm-backdrop--stacked");
    backdrop.appendChild(panel);
    backdrop.addEventListener("mousedown", function (e) {
      if (e.target === backdrop && handle.dismissible) closeModal(handle);
    });
    // 挂载到 #modal-host（各外壳都提供的模态挂载点）；页面没提供时退回 body。
    // 注意 $() 是 getElementById 的别名，传 id 不带 "#"，带前缀会永远取到 null。
    var host = $("modal-host");
    (host || document.body).appendChild(backdrop);
    handle.backdrop = backdrop;
    modalStack.push(handle);
    lockScroll();
    backdrop.hidden = false;
    if (reducedMotion()) backdrop.classList.add("is-open");
    else requestAnimationFrame(function () { backdrop.classList.add("is-open"); });
    var first = focusables(panel)[0];
    if (first) first.focus(); else panel.focus();
    if (typeof cfg.onOpen === "function") cfg.onOpen(handle);
    return handle;
  }
  function adoptModal(node, trigger) {
    var existing = modalStack.filter(function (m) { return m.el === node; })[0];
    if (existing) return existing;
    trigger = trigger || document.activeElement;
    node.classList.remove("hidden");
    var handle = {
      el: node, panel: node, backdrop: null, trigger: trigger,
      dismissible: true, _adopted: true,
      close: function () { closeModal(handle); },
      setTitle: function (t) { var tt = node.querySelector(".modal-title"); if (tt) tt.textContent = t; },
      setBody: function (b) {
        var body = node.querySelector(".modal-body") || node;
        body.innerHTML = ""; appendBody(body, b);
      }
    };
    modalStack.push(handle);
    lockScroll();
    var first = focusables(node)[0];
    if (first) first.focus(); else if (typeof node.focus === "function") node.focus();
    return handle;
  }
  function closeModal(target) {
    var handle;
    if (!target) handle = modalStack[modalStack.length - 1];
    else if (target.nodeType === 1) handle = modalStack.filter(function (m) { return m.el === target; })[0];
    else handle = modalStack.indexOf(target) !== -1 ? target : null;
    if (!handle) return false;
    modalStack = modalStack.filter(function (m) { return m !== handle; });
    if (handle._adopted) {
      handle.el.classList.add("hidden");
    } else if (handle.backdrop) {
      var bd = handle.backdrop;
      bd.classList.remove("is-open");
      setTimeout(function () {
        bd.hidden = true;
        if (bd.parentNode) bd.parentNode.removeChild(bd);
      }, reducedMotion() ? 0 : 200);
    }
    unlockScroll();
    if (typeof handle.onClose === "function") { try { handle.onClose(); } catch (e) {} }
    var trig = handle.trigger;
    if (trig && document.contains(trig) && typeof trig.focus === "function") trig.focus();
    return true;
  }
  function trapTab(e, handle) {
    var root = handle.panel || handle.el;
    var items = focusables(root);
    if (!items.length) { e.preventDefault(); if (root.focus) root.focus(); return; }
    var first = items[0], last = items[items.length - 1];
    if (!root.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function confirmDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var settled = false;
      function done(v) { if (!settled) { settled = true; resolve(v); } }
      openModal({
        title: opts.title || "请确认",
        body: el("div", { class: "pm-confirm-text", text: opts.body || "" }),
        dismissible: opts.dismissible !== false,
        onClose: function () { done(false); },
        actions: [
          { label: opts.cancelText || "取消", variant: "ghost", onClick: function () { done(false); } },
          { label: opts.confirmText || "确定", variant: opts.danger ? "danger" : "primary", onClick: function () { done(true); } }
        ]
      });
    });
  }
  function promptDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var settled = false;
      function done(v) { if (!settled) { settled = true; resolve(v); } }
      var inputId = "pm-prompt-" + (++uidSeq);
      var input = el("input", {
        id: inputId, class: "input", type: opts.password ? "password" : "text",
        placeholder: opts.placeholder || "", maxlength: opts.maxlength || null,
        autocomplete: opts.autocomplete || "off"
      });
      if (opts.defaultValue != null) input.value = opts.defaultValue;
      var err = el("div", { class: "field-error", hidden: true });
      var field = el("div", { class: "field" });
      if (opts.label) field.appendChild(el("label", { class: "field-label", for: inputId, text: opts.label }));
      field.appendChild(input);
      field.appendChild(err);
      openModal({
        title: opts.title || "请输入",
        body: field,
        dismissible: opts.dismissible !== false,
        onClose: function () { done(null); },
        onOpen: function () { input.focus(); if (input.select) input.select(); },
        actions: [
          { label: opts.cancelText || "取消", variant: "ghost", onClick: function () { done(null); } },
          {
            label: opts.confirmText || "确定", variant: "primary",
            onClick: function () {
              var v = input.value;
              if (opts.required && !String(v).trim()) {
                err.textContent = opts.requiredMessage || "此项为必填";
                err.hidden = false;
                input.classList.add("is-invalid");
                input.focus();
                return false; // 阻止关闭
              }
              done(v);
            }
          }
        ]
      });
    });
  }

  /* ---------- 设置页就地状态条（.set-tip / .set-bad，语义见 app.css） ---------- */
  // 系统设置页各分区组件（settings-* / work_settings 公告区）共用的唯一实现：
  // 写 textContent + 切换 className 两步即全部语义，bad 时追加 set-bad 失败配色。
  // 元素缺失是常态（分区未渲染/无该条），静默返回。
  function setTip(elId, text, bad) {
    var n = $(elId);
    if (!n) return;
    n.textContent = text || "";
    n.className = bad ? "set-tip set-bad" : "set-tip";
  }

  /* ---------- 口令策略（管理端与后端 web/app.py 同一口径） ---------- */
  // 全站唯一一份口令判定：登录/注册页、用户自助改密、管理端重置/新增口令都从这里取，
  // 不再各自内联一份数组（历史上多份副本互相漂移过）。
  // 与后端 _PASSWORD_CLASS_PATTERNS / _PASSWORD_POLICY_HINT 逐字同序同串：判定语义是
  // "命中类别数 >= PW_MIN_CLASSES 即过"（符号自成一类，不额外要求必须含符号）。
  // tests/test_rekey_key_source.py 通过 frontend_source 聚合本文件与后端常量比对，
  // 故这里必须用 const 声明（元测试按 `const PW_CLASS_PATTERNS = [...]` 提取）。
  const PW_CLASS_PATTERNS = [/[A-Z]/, /[a-z]/, /\d/, /[^A-Za-z0-9]/];
  const PW_MIN_LEN = 10, PW_MIN_CLASSES = 2;
  const PW_POLICY_HINT = "至少 10 位，且包含大小写字母、数字、符号中的至少两类";
  // 内置主管理员（.env）口令单独提档：12 位三类，与后端 _admin_password_policy_error 同口径
  const PW_ADMIN_MIN_LEN = 12, PW_ADMIN_MIN_CLASSES = 3;
  const PW_ADMIN_HINT = "至少 12 位，且包含大写字母、小写字母、数字、符号中的至少三类";
  function passwordClasses(v) {
    var s = String(v == null ? "" : v);
    return PW_CLASS_PATTERNS.filter(function (re) { return re.test(s); }).length;
  }
  function passwordPolicyOk(v) {
    var s = String(v == null ? "" : v);
    return s.length >= PW_MIN_LEN && passwordClasses(s) >= PW_MIN_CLASSES;
  }
  function passwordPolicyOkAdmin(v) {
    var s = String(v == null ? "" : v);
    return s.length >= PW_ADMIN_MIN_LEN && passwordClasses(s) >= PW_ADMIN_MIN_CLASSES;
  }

  /* ---------- 危险操作门禁失败的机器可读分类 ----------
     后端在门禁拒绝时下发 `reason`：password_required = 本次未提交口令（调用方应弹口令框
     后重试），password_incorrect = 口令输错（应提示输错并允许改口令重试），
     delay_ack_required = 不可逆操作缺少/伪造 confirm_delay_ack（应弹倒计时确认框）。
     档位（YIBAN_PW_GATE）只存在于后端，前端不判断档位、只认 reason；旧后端未下发 reason
     时回落为空串，按后端原文案显示，语义与既有契约一致。 */
  function pwGateReason(e) {
    var r = e && e.data && e.data.reason;
    return (r === "password_required" || r === "password_incorrect"
      || r === "delay_ack_required") ? r : "";
  }
  function pwGateMessage(e) {
    var r = pwGateReason(e);
    if (r === "password_required") return "此操作需要输入当前口令，请重新输入后确认。";
    if (r === "password_incorrect") return "当前口令不正确，请重新输入。";
    if (r === "delay_ack_required") return "此操作需要二次确认，请在倒计时结束后确认。";
    // 非口令门禁失败（冷却 429、无事可做 400、网络错误等）：原样显示后端文案
    return (e && e.message) || "操作失败，请稍后重试";
  }

  /* ---------- 密码模态（重置密码 / 高危操作二次确认共用） ---------- */
  // 动态构建在 openModal 之上：新 MPA 外壳不 include partials/modals/*，模态一律在运行时构造（沿用旧 DOM id 会 ReferenceError）。
  // partials/modals/*，故模态一律在运行时构造（沿用旧 DOM id 会 ReferenceError）。
  // set 模式走完整口令策略（长度 + 类别）；confirm 模式只验非空，当前口令由后端最终核对。
  // 动态文案（邮箱等）只经 el 的 text 选项写入 textContent，无 innerHTML 注入面。
  function openPasswordModal(desc, cb, onCancel) { return openPwModal(desc, cb, "set", onCancel); }
  function openConfirmPasswordModal(desc, cb, onCancel) { return openPwModal(desc, cb, "confirm", onCancel); }
  // onCancel（可选）：用户取消/关闭口令框时回调 —— 调用方据此把"未提交"这条路径走完
  // （例如把一次显式保存标记为取消，而不是让外层等待一个永不落定的 Promise）。
  function openPwModal(desc, cb, mode, onCancel) {
    var isConfirm = mode === "confirm";
    var submitted = false;
    var pending = false; // thenable 回调在途标记：期间忽略再次提交
    var inputId = "pm-pw-" + (++uidSeq);
    var input = el("input", {
      id: inputId, class: "input", type: "password",
      autocomplete: isConfirm ? "current-password" : "new-password",
      // placeholder 保持短句：手机端输入框内不换行，完整口径由可换行的 desc 承载
      placeholder: isConfirm ? "输入当前管理员密码" : "设置新密码（至少 10 位）"
    });
    var err = el("div", { class: "field-error", hidden: true });
    var field = el("div", { class: "field" });
    field.appendChild(el("div", { class: "pm-confirm-text", text: desc || "" }));
    field.appendChild(input);
    field.appendChild(err);
    function reject(msg) {
      err.textContent = msg;
      err.hidden = false;
      input.classList.add("is-invalid");
      input.focus();
      return false; // 返回 false 阻止 openModal 关闭
    }
    function setFootBusy(busy) {
      var foot = pwHandle.panel.querySelector(".modal-foot");
      if (!foot) return;
      [].forEach.call(foot.querySelectorAll(".btn"), function (b) { b.disabled = !!busy; });
    }
    function submit() {
      var pw = input.value;
      if (!pw) return reject("请输入密码");
      // set 模式按完整策略校验，与后端 _password_policy_error 同口径，
      // 避免"前端放行、提交后才 400"；confirm 模式只验非空。
      if (!isConfirm && !passwordPolicyOk(pw)) return reject("密码" + PW_POLICY_HINT);
      if (pending || submitted) return false;    // 在途或已提交：忽略重复提交（挡在调用回调之前）
      var fn = cb;
      // 回调**只调一次**：它的返回值决定后续走哪条路。
      // （老实现为判断"回不返回 Promise"会先探测调一次、再对非 Promise 回调调第二次 —— 回调会真的跑两遍：
      //  再在下方为非 Promise 回调调第二次 —— 那些回调会真的执行两遍：两次写请求（含两次审计）、
      //  实测端点则变成两次真实联网，前端限速形同不存在。）
      // `submitted || pending` 那道护栏还必须留在调用**之前**：非 Promise 回调返回后该次提交虽然
      // 立刻关上弹窗，但面板要等 200ms 才从 DOM 摘掉，真实鼠标双击的第二下仍能命中「确认操作」，
      // 于是同一份口令会再发一次请求（实测复现）。
      var result = fn ? fn(pw) : undefined;
      if (result && typeof result.then === "function") {
        // 回调返回 Promise：弹窗保持打开直至请求落定——拒绝时经 reject() 把错误显示在弹窗内
        // （口令框保留原值，可直接改口令重试），期间禁用底部按钮防重复提交
        pending = true;
        setFootBusy(true);
        result.then(function () {
          submitted = true;
          closeModal(pwHandle);
        }, function (e) {
          pending = false;
          setFootBusy(false);
          // 口令门禁失败按 reason 分支：缺口令 → 提示重新输入；输错 → 明说"不正确"，
          // 两种情况都留在框内可重试（弹窗不关闭）。
          reject(pwGateMessage(e));
        });
        return false;
      }
      submitted = true;
      closeModal(pwHandle); // 收尾：本层关掉，调用方若紧接着叠开第二个模态，栈序仍是新层在上
      return false;         // 已手动关闭
    }
    var pwHandle = openModal({
      title: isConfirm ? "安全确认" : "重置密码",
      body: field,
      onOpen: function () { input.focus(); },
      onClose: function () { if (!submitted && onCancel) onCancel(); },
      actions: [
        { label: "取消", variant: "ghost" },
        { label: isConfirm ? "确认操作" : "确认重置", variant: "primary", onClick: submit }
      ]
    });
    return pwHandle;
  }

  /* ---------- 不可逆操作的倒计时确认框 ----------
     软性摩擦的落地形态：把「输口令打断心流」换成「等几秒再确认」，靠时间成本挡手滑连点，
     而不是要求现场回忆管理员口令。按钮在倒计时内禁用并显示剩余秒数（5s → 4s → …），
     归零后启用；确认 resolve(true)，取消/关闭 resolve(false)。
     倒计时秒数只在前端生效（后端不校验秒数，真正兜底是配额 + 事后告警 + 审计），
     故这里的定时器必须在两条收尾路径（确认 / 取消关闭）上都清掉，否则弹窗关了还在空转。 */
  var DELAY_ACK_SECONDS = 5;
  function delayAckLabel(left) { return left > 0 ? "确认（" + left + "s）" : "确认执行"; }
  function openDelayAckModal(desc, confirmText) {
    return new Promise(function (resolve) {
      var settled = false, timer = null;
      function settle(v) {
        if (settled) return;
        settled = true;
        if (timer) { clearInterval(timer); timer = null; }
        resolve(v);
      }
      var handle = openModal({
        title: "不可逆操作确认",
        body: el("div", { class: "pm-confirm-text", text: desc || "此操作不可逆，请确认。" }),
        onClose: function () { settle(false); },
        actions: [
          { label: "取消", variant: "ghost" },
          { label: confirmText || "确认执行", variant: "danger", onClick: function () { settle(true); } }
        ]
      });
      var foot = handle.panel.querySelector(".modal-foot");
      var okBtn = foot ? foot.querySelector(".btn--danger") : null;
      if (!okBtn) { settle(false); return; }
      var left = DELAY_ACK_SECONDS;
      okBtn.disabled = true;
      okBtn.textContent = delayAckLabel(left);
      timer = setInterval(function () {
        left -= 1;
        if (left <= 0) {
          clearInterval(timer);
          timer = null;
          okBtn.disabled = false;
          okBtn.textContent = delayAckLabel(0);
          return;
        }
        okBtn.textContent = delayAckLabel(left);
      }, 1000);
    });
  }

  /* ---------- 受门禁操作的统一提交入口 ----------
     档位（YIBAN_PW_GATE）只存在于后端，**受门禁操作一律先不带任何凭据发**：本处不判断
     档位、也不预判"要不要口令"，只按响应体的 reason 分流——
       · delay_ack_required → 弹倒计时确认框，确认后带 confirm_delay_ack: true 重发；
       · password_required / password_incorrect → 弹既有口令框，口令随重发提交；
       · 其余失败原样上抛，由调用方的失败处理接管。
     这样每个调用点都不必自己拼口令框管道，也不必猜后端档位（猜错就是"用户白输一次
     口令"或"请求被 403 打回"）。倒计时只对不可逆操作出现——后端只对它们下发
     delay_ack_required，非不可逆操作带上该字段也不会被要求。
     口令与倒计时凭据各只自动补一次：后端再次拒绝即上抛，绝不无限重发；口令错的那次
     由口令框自身在框内提示并允许改口令重试（沿用既有流程）。用户取消任一弹窗时以带
     canceled 标记的错误拒绝。取消与被后端打回都**不等于什么都没发生**：多段提交里
     先成功的步骤已经落库，故两种失败都另带 `completed`（已成功提交的步数），调用方
     据此刷新视图并说明已生效的部分不会回滚（见 components/settings-executors.js 的
     canceledAfter / failedAfter）。 */
  function dangerousSubmit(opts) {
    // 一次点击要按序发**多个**受门禁请求时用 opts.requests（[{method, path, body}, …]），
    // 否则用单个 path/body。凭据对整串共用，且**从失败那一步继续**、已成功的步骤不重发，
    // 故一次点击最多问一次口令——否则"改出口 + 同时拨故障转移开关"这类保存会连弹两次框。
    // requests 形式 resolve 各步响应组成的数组（调用方按步取 note），单请求形式 resolve 该响应。
    var multi = !!(opts.requests && opts.requests.length);
    var steps = multi ? opts.requests.slice() : [{ method: opts.method, path: opts.path, body: opts.body }];
    var results = [];
    var triedPw = false, triedAck = false;
    function merged(base, extra) {
      var out = {}, keys = Object.keys(base || {}), i;
      for (i = 0; i < keys.length; i++) out[keys[i]] = base[keys[i]];
      if (extra) { keys = Object.keys(extra); for (i = 0; i < keys.length; i++) out[keys[i]] = extra[keys[i]]; }
      return out;
    }
    function withExtra(extra, add) {
      var out = {}, keys = Object.keys(extra || {}), i;
      for (i = 0; i < keys.length; i++) out[keys[i]] = extra[keys[i]];
      keys = Object.keys(add);
      for (i = 0; i < keys.length; i++) out[keys[i]] = add[keys[i]];
      return out;
    }
    function canceled() {
      // 已成功提交的步数随取消一起回传：多段提交的调用方要据此判断"库里是不是已经有
      // 一半改动"，只给 canceled 布尔值会让那半次写入没有出口（用户看不见、也不提示）。
      // results 由 step() 按步号写入，故其长度就是已落库的步数。
      var e = new Error("");
      e.canceled = true;
      e.completed = results.length;
      return e;
    }
    function step(i, extra) {
      var s = steps[i];
      return api(s.method || "POST", s.path, merged(s.body, extra)).then(function (data) {
        results[i] = data;
        if (i + 1 < steps.length) return step(i + 1, extra);
        return multi ? results : data;
      }, function (e) {
        var r = pwGateReason(e);
        if (r === "delay_ack_required") {
          if (triedAck) throw e;
          triedAck = true;
          return openDelayAckModal(opts.delayDesc || opts.desc, opts.confirmText).then(function (ok) {
            if (!ok) throw canceled();
            return step(i, withExtra(extra, { confirm_delay_ack: true }));
          });
        }
        if (r === "password_required" || r === "password_incorrect") {
          if (triedPw) throw e;
          triedPw = true;
          return new Promise(function (resolve, reject) {
            openConfirmPasswordModal(opts.desc, function (pw) {
              // 回调返回 Promise：口令框保持打开直至请求落定；拒绝时在框内提示并可改口令重试
              return step(i, withExtra(extra, { confirm_password: pw }))
                .then(resolve, function (e2) { throw e2; });
            }, function () { reject(canceled()); });
          });
        }
        throw e;
      });
    }
    // 非取消的失败也带上已落库的步数：多段提交在第 2 步被打回时第 1 步已经写进库，调用方
    // 要据此重载视图并交代已提交的部分——与 canceled 同一口径，否则那半次写入没人提示
    // （见 components/settings-executors.js 的 failedAfter）。
    return step(0, null).catch(function (e) {
      if (e && !e.canceled) e.completed = results.length;
      throw e;
    });
  }

  /* ---------- 主题 ---------- */
  function currentTheme() { return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light"; }
  function updateThemeIcons(theme) {
    var name = (theme || currentTheme()) === "dark" ? "sun" : "moon";
    forEach(document.querySelectorAll("#themeToggle, [data-theme-btn]"), function (btn) { btn.innerHTML = svgUse(name); });
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

  /* ---------- 下拉菜单 ---------- */
  function closeDropdowns(except) {
    forEach(document.querySelectorAll(".dd-wrap.is-open"), function (w) { if (w !== except) w.classList.remove("is-open"); });
  }
  function focusItem(items, index) {
    if (!items.length) return;
    items[((index % items.length) + items.length) % items.length].focus();
  }
  // 顶栏下拉的视口钳制。vendor 在 ≤720 把 .dd-menu 定成 width:calc(100vw - 16px) 且 right:-8px,
  // 面板右缘锚在**触发器右缘**；通知铃铛不是顶栏最后一个元素，锚点离视口右缘还有一段距离，
  // 面板就从左侧越界（实测 390 下 left=-50、320 下 left=-64）。纯 CSS 改不了：面板宽度由
  // 锚点右缘反推，锚点位置随顶栏排布（铃铛/主题/头像）变化，`max-width` 也管不到左溢出。
  // 故与自研日期弹层同一套思路——按「目标视口位置 − 锚点」一次算准，只保留 left 一种锚定：
  //   候选左缘 = 锚点右缘 − 面板宽，再夹进 [PAD, vw − PAD − 面板宽]。
  var DD_PAD = 8;
  function clampDropdown(wrap) {
    var menu = wrap.querySelector(".dd-menu");
    if (!menu) return;
    // 行内操作下拉由 YB.rowMenu 用 fixed 自己钳制（portal 到 body），不在此处理
    if (wrap.classList.contains("acct-row-menu") || wrap.classList.contains("usr-row-menu")) return;
    menu.style.left = "";
    menu.style.right = "";
    var vw = document.documentElement.clientWidth || window.innerWidth || 0;
    // 闭态带 scale(0.98)，矩形会缩水，量 offsetWidth（不受 transform 影响）
    var w = menu.offsetWidth;
    if (!w || !vw) return;
    // 内联已清空，此处的 computed right 即 CSS 的锚定偏移（桌面 0 / 移动 -8px），
    // 用它还原「菜单右缘」的锚点意图（right:-8 → 右缘 = 锚点右缘 + 8）
    var rightOff = parseFloat(window.getComputedStyle(menu).right);
    if (!isFinite(rightOff)) rightOff = 0;
    var wrapRect = wrap.getBoundingClientRect();
    var anchorRight = wrapRect.right - rightOff;
    var left = Math.min(Math.max(anchorRight - w, DD_PAD), Math.max(DD_PAD, vw - DD_PAD - w));
    menu.style.left = Math.round(left - wrapRect.left) + "px";
    menu.style.right = "auto";
  }
  function clampOpenDropdowns() {
    forEach(document.querySelectorAll(".dd-wrap.is-open"), clampDropdown);
  }
  function toggleDropdown(trigger) {
    var wrap = trigger && trigger.closest ? trigger.closest(".dd-wrap") : null;
    if (!wrap) return;
    var willOpen = !wrap.classList.contains("is-open");
    closeDropdowns(wrap);
    wrap.classList.toggle("is-open", willOpen);
    if (willOpen) clampDropdown(wrap);
  }

  /* ---------- 导航分组（桌面手风琴 + 721–1100px rail 浮层定位） ---------- */
  function isRailMode() { return window.innerWidth > 720 && window.innerWidth <= 1100; }
  function positionRailFlyout(sub, trigger) {
    var r = trigger.getBoundingClientRect();
    sub.style.top = Math.max(8, Math.min(r.top, window.innerHeight - 60)) + "px";
  }
  function relayoutRail() {
    var groups = document.querySelectorAll("[data-nav-group].is-open");
    if (!isRailMode()) { forEach(groups, function (g) { var s = g.querySelector(".nav-submenu"); if (s) s.style.top = ""; }); return; }
    forEach(groups, function (g) {
      var s = g.querySelector(".nav-submenu"), t = g.querySelector("[data-nav-toggle]");
      if (s && t) positionRailFlyout(s, t);
    });
  }
  /* ---------- Tab（容器 [data-tab-group] + .tab[data-tab-target] + .tab-panel[data-tab-id]） ---------- */
  // 深链参数名与同步口径自 /work/settings 的原实现提炼（全站唯一一份）：?tab=<分区 id>，
  // 切换时 history.replaceState 同步（不产生历史堆积）。settings 页分区切换带脏守卫，
  // 写 URL 的时机必须等守卫放行 —— 该组在模板上标 data-tab-url-own（自管），委托路径
  // 不写 URL，由页面经 selectTab(…, { syncUrl: true }) 显式写；其余页面每次激活即同步。
  var TAB_URL_PARAM = "tab";
  function tabFromUrl() {
    try { return new URLSearchParams(location.search).get(TAB_URL_PARAM); } catch (e) { return null; }
  }
  function tabSyncUrl(name) {
    try {
      var u = new URL(location.href);
      u.searchParams.set(TAB_URL_PARAM, name);
      history.replaceState(null, "", u.pathname + u.search + u.hash);
    } catch (e) { /* 受限环境忽略 */ }
  }
  // 可见性校验（settings 原 validTab 语义）：存在未 hidden 的同名 tab 才允许深链/切换。
  // 遍历所有匹配而非只看第一个 —— 多分组时第一个匹配可能 hidden 而后续可见。
  function tabVisible(name) {
    var found = false;
    forEach(document.querySelectorAll('[data-tab-group] .tab[data-tab-target="' + cssEscape(name) + '"]'), function (t) {
      if (!t.hidden) found = true;
    });
    return found;
  }
  function activateTab(group, target, opts) {
    opts = opts || {};
    forEach(group.querySelectorAll(".tab[data-tab-target]"), function (t) {
      var on = t.getAttribute("data-tab-target") === target;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      // roving tabindex（WAI-ARIA tabs）：活动 tab 是 Tab 键序唯一停止点，其余 -1 仍可点击聚焦
      t.setAttribute("tabindex", on ? "0" : "-1");
    });
    forEach(group.querySelectorAll(".tab-panel[data-tab-id]"), function (p) {
      var on = p.getAttribute("data-tab-id") === target;
      p.classList.toggle("is-active", on);
      // 键盘方向键属高频切换：激活面板打 data-tab-instant 供 CSS 跳过进入动效；
      // 下一次鼠标/程序化激活会清掉标记，动画自动恢复（无需定时器清理）。
      if (on && opts.instant) p.setAttribute("data-tab-instant", "");
      else p.removeAttribute("data-tab-instant");
    });
    // 活动标签滚进视口：窄屏 tab 条会横向溢出，深链（?tab=switches）或程序化激活时
    // 活动 tab 可能整个在视口外（实测 scrollLeft=0、tab 在 x=493~573、可视到 344）。
    var activeTab = group.querySelector(".tab.is-active");
    if (activeTab && activeTab.scrollIntoView) {
      try { activeTab.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch (e) { /* 老浏览器忽略 */ }
    }
    if (!opts.skipUrl && (opts.syncUrl || !group.hasAttribute("data-tab-url-own"))) tabSyncUrl(target);
  }
  function cssEscape(s) { return String(s).replace(/["\\]/g, "\\$&"); }
  function switchTab(name, opts) {
    if (!name) return;
    var panel = document.querySelector('[data-tab-group] [data-tab-id="' + cssEscape(name) + '"]');
    if (panel) activateTab(panel.closest("[data-tab-group]"), name, opts);
    forEach(document.querySelectorAll('[id^="tab-"]'), function (p) { p.classList.toggle("hidden", p.id !== "tab-" + name); });
    forEach(document.querySelectorAll("[data-tab-btn]"), function (b) { b.classList.toggle("is-active", b.getAttribute("data-tab-btn") === name); });
    try { document.dispatchEvent(new CustomEvent("yiban:tab", { detail: { name: name } })); } catch (e) {}
  }
  // 校验并切换（settings 原 selectTab 语义）：opts.syncUrl 强制写 URL（自管分组），
  // opts.skipUrl / opts.instant 透传 activateTab。返回是否真的切换了。
  function selectTab(name, opts) {
    if (!tabVisible(name)) return false;
    switchTab(name, opts);
    return true;
  }
  // 页面 init 调用一次：读 ?tab= 直链选中对应分区。初始选中不写 URL —— 地址栏本就
  // 处于该状态（与 settings 原实现的 replaceState 时机一致）。
  function tabDeepLink() {
    var want = tabFromUrl();
    return !!(want && selectTab(want, { skipUrl: true }));
  }
  // 初始 roving tabindex 归一：模板已服务端渲染初始值，这里兜底修正模板漏写/动态分组。
  function initTabRoving() {
    forEach(document.querySelectorAll("[data-tab-group]"), function (g) {
      var tabs = Array.prototype.filter.call(g.querySelectorAll(".tab[data-tab-target]"), function (t) { return !t.hidden; });
      var active = tabs.filter(function (t) { return t.classList.contains("is-active"); })[0] || tabs[0];
      forEach(g.querySelectorAll(".tab[data-tab-target]"), function (t) {
        t.setAttribute("tabindex", t === active ? "0" : "-1");
      });
    });
  }

  /* ---------- 更新日志 ---------- */
  function openChangelog() {
    var bodyEl = el("div", { class: "md-body", text: "加载中…" });
    var handle = openModal({
      title: "更新日志", size: "lg", body: bodyEl,
      actions: [{ label: "关闭", variant: "ghost" }]
    });
    api("GET", "/api/changelog").then(function (data) {
      var text = (data && data.text) || "暂无更新日志";
      if (window.renderMarkdown) bodyEl.innerHTML = window.renderMarkdown(text); // 内部已转义
      else bodyEl.textContent = text;
    }).catch(function (err) {
      bodyEl.textContent = (err && err.message) || "加载失败，请稍后重试";
    });
    return handle;
  }

  /* ---------- 退出 ---------- */
  function doLogout() {
    return api("POST", "/api/logout").catch(function () {}).then(function () {
      location.href = url("/login");
    });
  }

  /* ---------- 身份 ---------- */
  var me = null;
  var mePending = null;
  function roleLabel(m) {
    if (!m) return "";
    if (m.is_builtin_admin) return "主管理员";
    return m.role === "admin" ? "管理员" : m.role === "user" ? "普通用户" : (m.role || "");
  }
  function hydrateIdentity() {
    // 外壳身份走 30s 会话缓存：切页不再重复拉 /api/me（CSRF token 随会话稳定，
    // 缓存内一并带回；写请求成功会清缓存，角色变更最多滞后 30s）。
    return apiCached("me", 30000, function () { return api("GET", "/api/me"); }).then(function (data) {
      me = data;
      if (data && data.csrf_token) csrfToken = data.csrf_token;
      var name = data.username || data.email || "";
      // 名字只留本身：角色由下一行 [data-account-role] 表达，不再重复后缀
      setText("[data-account-name]", name);
      setText("[data-account-email]", data.email || "");
      setText("[data-account-role]", roleLabel(data));
      var initial = String(data.username || data.email || "?").replace(/\s+/g, "").slice(0, 2).toUpperCase();
      setText("[data-account-avatar]", initial || "?");
      return data;
    }).catch(function () { return null; });
  }
  // 去重的身份读取：外壳初始化与页面脚本都要用 /api/me 的结果，共享一次请求。
  // 失败（401/网络）返回 null，调用方自行决定跳登录还是降级。
  function identity() {
    if (me) return Promise.resolve(me);
    if (!mePending) {
      mePending = hydrateIdentity().then(function (d) { mePending = null; return d; });
    }
    return mePending;
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
    setText("[data-clock-text]", s);
    setText("[data-clock-now]", s);
  }
  function reflectSignStatus() {
    if (!clock.status) return;
    forEach(document.querySelectorAll("[data-sign-status]"), function (n) {
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
    var doFetch = function () { return api("GET", "/api/clock"); };
    return (force ? doFetch() : apiCached("clock", 10000, doFetch)).then(function (data) {
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

  /* ---------- 导航徽标（仅管理员） ---------- */
  function setNavBadge(key, count) {
    forEach(document.querySelectorAll('[data-nav-badge="' + cssEscape(key) + '"]'), function (node) {
      if (!count || count <= 0) { node.hidden = true; node.textContent = ""; return; }
      node.textContent = String(count); node.hidden = false;
    });
  }
  function loadNavBadges(identity) {
    if (!identity || identity.role !== "admin") return;
    // 导航徽标是外壳级低频数据：60s 缓存，切页不再重复拉 /api/accounts、/api/users
    // （审核/删除等写操作成功会整体清缓存，徽标随之即时重取）。
    apiCached("nav-accounts", 60000, function () { return api("GET", "/api/accounts"); }).then(function (data) {
      var list = (data && data.accounts) || [];
      // 徽标口径与账号管理页「待处理账号」组一致：待审核 + 已拒绝。
      // 只数 pending 会让徽标数小于页面里的待处理条数，同一条目两处不一致。
      setNavBadge("work-accounts", list.filter(function (a) {
        return a && !a.deleted && (a.status === "pending" || a.status === "rejected");
      }).length);
    }).catch(function () {});
    apiCached("nav-users", 60000, function () { return api("GET", "/api/users"); }).then(function (data) {
      var list = (data && data.users) || [];
      // 待处理用户 = 名下有「待审核或已拒绝」账号的用户数。
      // review_count 已是 pending+rejected 的超集，再叠加 pending_count 会重复计数。
      var review = list.filter(function (u) { return Number(u && u.review_count) > 0; }).length;
      setNavBadge("work-users", review);
    }).catch(function () {});
  }

  /* ---------- 公告 ---------- */
  function showAnnouncement(text) {
    openModal({ title: "公告", body: el("div", { class: "pm-announce", text: text }), actions: [{ label: "关闭", variant: "ghost" }] });
  }
  // 公告关闭只记在本次会话（sessionStorage），按文本区分：
  // 同一公告不再重复打扰，但换了内容会重新显示（避免"永久静音"掉重要通知）。
  var ANNOUNCE_DISMISS_KEY = "yiban-announce-dismissed";
  function announceIsDismissed(text) {
    try { return sessionStorage.getItem(ANNOUNCE_DISMISS_KEY) === text; } catch (e) { return false; }
  }
  function dismissAnnouncement(text) {
    try { sessionStorage.setItem(ANNOUNCE_DISMISS_KEY, text); } catch (e) {}
    forEach(document.querySelectorAll("[data-announcement-bar]"), function (bar) { bar.hidden = true; });
  }
  function showBell() {
    var btn = $("announcementBtn");
    if (!btn) return null;
    // data-notif-always：通知中心入口常驻（用户端下拉面板）；其余页面沿用"有公告才显示"
    if (btn.getAttribute("data-notif-always") === "1") btn.hidden = false;
    return btn;
  }
  // 公告文本落到全部挂点（顶栏入口、通知中心分节、登录页横幅）。单独抽出来是为了让
  // 「发布/下线」拿到响应里的 text 后能就地刷新横幅，不必再等一次 GET（且不再重复绑定监听：
  // 用 data-announce-bound / data-announce-text 幂等绑定与取值）。
  function applyAnnouncementText(raw) {
    var text = String(raw == null ? "" : raw).trim();
    var btn = $("announcementBtn");
    if (btn && !btn.closest(".dd-wrap")) {
      btn.setAttribute("data-announce-text", text);
      if (text) {
        btn.hidden = false;
        if (btn.getAttribute("data-announce-bound") !== "1") {
          btn.setAttribute("data-announce-bound", "1");
          btn.addEventListener("click", function () {
            showAnnouncement(String(btn.getAttribute("data-announce-text") || ""));
          });
        }
      } else {
        btn.hidden = true;
      }
    }
    showBell();
    var dot = document.querySelector("[data-announcement-dot]");
    if (dot) dot.hidden = !text;
    forEach(document.querySelectorAll("[data-announcement-text]"), function (n) { n.textContent = text; });
    forEach(document.querySelectorAll("[data-announcement-block]"), function (block) { block.hidden = !text; });
    forEach(document.querySelectorAll("[data-announcement-bar]"), function (bar) {
      var closeBtn = bar.querySelector("[data-announcement-dismiss]");
      if (closeBtn && closeBtn.getAttribute("data-announce-bound") !== "1") {
        closeBtn.setAttribute("data-announce-bound", "1");
        closeBtn.addEventListener("click", function () {
          var tn = bar.querySelector("[data-announcement-text]");
          dismissAnnouncement(tn ? (tn.textContent || "") : "");
        });
      }
      bar.hidden = !text || announceIsDismissed(text);
    });
  }
  function initAnnouncement() {
    // 公告是公开只读、低频变更：60s 缓存，切页不重复拉取
    apiCached("announcement", 60000, function () { return api("GET", "/api/announcement"); })
      .then(function (data) { applyAnnouncementText(data && data.text); })
      .catch(function () {
        // 公告接口失败也不能让通知入口消失（用户端下拉是常驻入口）
        showBell();
      });
  }

  /* ---------- 全局事件委托 ---------- */
  function initGlobalHandlers() {
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        if (modalStack.length) { e.preventDefault(); closeModal(); return; }
        if (document.body.classList.contains("has-drawer-open")) { toggleDrawer(false); return; }
        closeDropdowns();
        return;
      }
      if (e.key === "Tab" && modalStack.length) trapTab(e, modalStack[modalStack.length - 1]);
    });

    document.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var drawerOpen = t.closest("[data-drawer-open]");
      if (drawerOpen) { e.preventDefault(); toggleDrawer(true); return; }
      if (t.closest("[data-drawer-close]")) { toggleDrawer(false); return; }
      var drawerLink = t.closest(".d-sidebar a[href]");
      if (drawerLink && !drawerLink.hasAttribute("data-nav-toggle") && window.innerWidth <= 720) toggleDrawer(false);

      var ddTrigger = t.closest("[data-dropdown]");
      if (ddTrigger) { e.preventDefault(); toggleDropdown(ddTrigger); return; }
      if (t.closest(".dd-menu-item")) { closeDropdowns(); return; }
      if (!t.closest(".dd-wrap")) closeDropdowns();

      // 长口径说明走弹窗，不走 .info-pop 浮层：浮层靠 hover/focus 维持且 pointer-events:none
      // （见 app.css 该处注释），实测 495 字在 390 宽下高 747px、底边超视口 344px，
      // 超出部分既滚不到也选不中。克隆出来喂给弹窗，原节点留在页面里供下次再取。
      var docBtn = t.closest("[data-doc]");
      if (docBtn) {
        e.preventDefault();
        var docSrc = document.getElementById(docBtn.getAttribute("data-doc"));
        if (!docSrc) return;
        var docBody = docSrc.cloneNode(true);
        docBody.removeAttribute("hidden");
        docBody.classList.add("pm-doc");
        openModal({
          title: docBtn.getAttribute("data-doc-title") || "说明",
          body: docBody,
          actions: [{ label: "关闭", variant: "ghost" }]
        });
        return;
      }

      var navToggle = t.closest("[data-nav-toggle]");
      if (navToggle) {
        e.preventDefault();
        var group = navToggle.closest("[data-nav-group]");
        if (!group) return;
        var willOpen = !group.classList.contains("is-open");
        if (isRailMode()) {
          forEach(document.querySelectorAll("[data-nav-group].is-open"), function (g) { if (g !== group) g.classList.remove("is-open"); });
        }
        group.classList.toggle("is-open", willOpen);
        if (willOpen && isRailMode()) {
          var sub = group.querySelector(".nav-submenu");
          if (sub) positionRailFlyout(sub, navToggle);
        }
        return;
      }
      if (isRailMode() && !t.closest("[data-nav-group]")) {
        forEach(document.querySelectorAll("[data-nav-group].is-open"), function (g) { g.classList.remove("is-open"); });
      }

      var tab = t.closest('.tab[data-tab-target]');
      if (tab) {
        var grp = tab.closest("[data-tab-group]");
        if (grp) { e.preventDefault(); activateTab(grp, tab.getAttribute("data-tab-target")); }
        return;
      }
      var acc = t.closest("[data-accordion-trigger]");
      if (acc) { var item = acc.closest("[data-accordion]"); if (item) item.classList.toggle("is-open"); }
    });

    document.addEventListener("keydown", function (e) {
      var wrap = document.querySelector(".dd-wrap.is-open");
      var active = document.activeElement;
      // 分区 tab 的方向键导航（WAI-ARIA tabs 模式）：焦点在 .tab 上时接管左右/Home/End，
      // 切换分区并把焦点移到新 tab；未命中时完全不影响其它键盘行为。
      var tabEl = active && active.closest ? active.closest(".tab[data-tab-target]") : null;
      if (tabEl) {
        var tgroup = tabEl.closest("[data-tab-group]");
        if (tgroup) {
          var tabList = Array.prototype.filter.call(
            tgroup.querySelectorAll(".tab[data-tab-target]"),
            function (x) { return !x.hidden; }
          );
          var ti = tabList.indexOf(tabEl), to = null;
          if (e.key === "ArrowRight") to = tabList[(ti + 1) % tabList.length];
          else if (e.key === "ArrowLeft") to = tabList[(ti - 1 + tabList.length) % tabList.length];
          else if (e.key === "Home") to = tabList[0];
          else if (e.key === "End") to = tabList[tabList.length - 1];
          if (to) {
            e.preventDefault();
            // instant：键盘高频切换跳过面板进入动效（与 prefers-reduced-motion 同口径）
            activateTab(tgroup, to.getAttribute("data-tab-target"), { instant: true });
            to.focus();
            return;
          }
        }
      }
      if (!wrap) {
        if (active && active.matches && active.matches("[data-dropdown]") && active.tagName !== "BUTTON" &&
            (e.key === "Enter" || e.key === " " || e.key === "ArrowDown")) {
          e.preventDefault();
          toggleDropdown(active);
          focusItem(active.closest(".dd-wrap").querySelectorAll(".dd-menu-item"), 0);
        }
        return;
      }
      var items = wrap.querySelectorAll(".dd-menu-item");
      var idx = Array.prototype.indexOf.call(items, active);
      if (e.key === "ArrowDown") { e.preventDefault(); focusItem(items, idx + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); focusItem(items, idx - 1); }
      else if (e.key === "Home") { e.preventDefault(); focusItem(items, 0); }
      else if (e.key === "End") { e.preventDefault(); focusItem(items, items.length - 1); }
    });

    window.addEventListener("resize", function () { relayoutRail(); clampOpenDropdowns(); if (window.innerWidth > 720) toggleDrawer(false); });
    window.addEventListener("scroll", relayoutRail, true);
  }

  /* ---------- 回到顶部（共享） ----------
     列表页的表格没有内滚上限、长列表整页滚动，因此需要一步回顶的入口。
     按钮固定在右下、滚动超过阈值才出现；只动 opacity/transform（无布局动画），
     reduced-motion 去位移、点击直接回顶（不做平滑滚动）。键盘可达（原生 button + aria-label）。 */
  var TO_TOP_AT = 400;
  function initBackToTop() {
    if ($("to-top")) return;
    var btn = el("button", { type: "button", id: "to-top", class: "to-top", "aria-label": "回到顶部", title: "回到顶部" });
    btn.appendChild(el("span", { class: "to-top__icon", "aria-hidden": "true", html: svgUse("arrow-up") }));
    (document.body || document.documentElement).appendChild(btn);
    function sync() {
      var y = window.pageYOffset || document.documentElement.scrollTop || 0;
      btn.classList.toggle("is-show", y > TO_TOP_AT);
    }
    btn.addEventListener("click", function () {
      if (reducedMotion()) window.scrollTo(0, 0);
      else window.scrollTo({ top: 0, behavior: "smooth" });
      btn.blur();   // 回顶后按钮隐去，焦点归还文档主体，避免焦点停在不可见节点
    });
    window.addEventListener("scroll", sync, { passive: true });
    sync();
  }

  onReady(function () {
    initGlobalHandlers();
    initTabRoving();
    initNavProgress();
    initBackToTop();
    var themeBtn = $("themeToggle");
    if (themeBtn) themeBtn.addEventListener("click", toggleTheme);
    updateThemeIcons();
    // 公告是公开只读接口，认证页也要显示，故放在提前返回之前。
    initAnnouncement();
    // 认证页（layout_auth.html 的 data-page="auth"）没有身份/时钟需求，而 /api/me 与
    // /api/clock 对匿名请求返回 401；跳过可避免登录页每次加载产生无谓的失败请求。
    if (document.body.getAttribute("data-page") === "auth") return;
    identity().then(function (data) { if (data) loadNavBadges(data); });
    calibrateClock();
    renderClock();
    setInterval(renderClock, 1000);
    // 30s 强制刷新一次校准（force 绕过 10s 缓存，拿到最新 sign_status/颜色）
    setInterval(function () { calibrateClock({ force: true }); }, 30000);
  });

  /* ---------- 慢请求的「整体淡出 → 换内容 → 淡入」 ----------
     统一口径：加 is-swapping → 等退出过渡跑完（主元素的 transitionend 或 SWAP_MS
     兜底，先到者）→ 执行 render 回调 → 再移除 is-swapping 走进入过渡。
     为什么不能同帧/单帧移除：移除类会立刻取消 opacity 过渡，内容在 opacity 只掉到
     0.4–0.7 时就被拉回，看起来只是闪一下。主元素取数组首项（其 160ms 过渡即交换窗口），
     其余元素只同步切换类名，避免较短的过渡（如月份标题 120ms）提前打断主元素淡出。 */
  var SWAP_MS = 160;
  function swapOut(el, cb) {
    var els = [].concat(el || []).filter(Boolean);
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

  /* ---------- 顶部导航进度条（MPA 页面切换） ----------
     多页应用没有前端路由，点内部链接即整页跳转；浏览器自身不提供任何"正在导航"
     反馈，弱网下会出现"点了一下没反应 → 突然白屏换页"的跳变感。
     本模块在捕获阶段监听合格的同源导航点击，立即显示细进度条并缓慢推进；
     新页面 core.js 载入时读 sessionStorage 里的起点时间，接续补到 100% 再淡出。
     不合格的链接一律放行：修饰键（新标签/下载）、target!=_self、download、
     外链、仅 hash 同页、文件类扩展名、显式 data-no-nav-progress。
     reduced-motion 直接不显示（不做任何位移动画）。
     明确不做「预取」：页面响应是 Cache-Control: no-store（见 app.py _NO_STORE_PAGES），
     <link rel=prefetch> 拉到的整页无法在导航时复用，只会在单 worker 上白跑一遍
     渲染；各页共享的 JS/CSS 本身已带版本号缓存。 */
  var NAV_PROGRESS_KEY = "yiban-nav-progress-at";
  var navBar = null, navTimer = null, navValue = 0, navGuardTimer = null;
  // 兜底收尾：进度条靠**新页面**的 core.js 补到 100%，若那次点击最终没发生导航
  // （被页面自身逻辑拦下、或浏览器取消了导航），条会永远停在 90%。
  // 兜底计时器保证任何情况下都会自行收尾，不留下"一直在加载"的假象。
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
    navBar = el("div", { class: "nav-progress", "aria-hidden": "true" });
    navBar.appendChild(el("div", { class: "nav-progress__fill" }));
    (document.body || document.documentElement).appendChild(navBar);
    return navBar;
  }
  function navSetWidth(pct) { if (navBar) navBar.firstChild.style.width = pct + "%"; }
  function startNavProgress() {
    if (reducedMotion()) return;
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
    // 兜底：到点仍未发生导航（本页还在）就自行收尾
    clearTimeout(navGuardTimer);
    navGuardTimer = setTimeout(function () {
      if (document.visibilityState !== "hidden") finishNavProgress();
    }, NAV_GUARD_MS);
  }
  function finishNavProgress() {
    clearTimeout(navGuardTimer);
    if (!navBar || reducedMotion()) return;
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
    if (!reducedMotion() && startedAt && Date.now() - startedAt < 15000) {
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
    // 真正发生导航时（新文档即将接管）取消兜底：条的去向由新页面负责，不再自行收尾；
    // 从往返缓存恢复时（back/forward）把可能残留的条清掉，避免"回来还挂着"。
    window.addEventListener("pagehide", finishNavProgress);
    window.addEventListener("pageshow", function (e) { if (e.persisted) finishNavProgress(); });
  }

  /* ---------- 浏览器级显示偏好（localStorage） ----------
     仅影响本机显示密度，不涉及任何后端策略，故与 yiban-theme 同层使用 localStorage。
     归属邮箱开关：账号表窄屏在名称单元格内补一行归属邮箱，由本偏好控制显隐；
     默认开（键缺失=开），关闭后宽屏归属列不受影响。取值点集中在
     components/account-table.js 一处，改后下次渲染即生效（无需后端往返）。 */
  var PREF_OWNER_EMAIL = "yiban-owner-email";
  function ownerEmailVisible() {
    try { return localStorage.getItem(PREF_OWNER_EMAIL) !== "0"; } catch (e) { return true; }
  }
  function setOwnerEmailVisible(v) {
    try { localStorage.setItem(PREF_OWNER_EMAIL, v ? "1" : "0"); } catch (e) {}
    try { document.dispatchEvent(new CustomEvent("yiban:owner-email-pref", { detail: { visible: !!v } })); } catch (e) {}
  }

  /* ---------- 公开面 ---------- */
  var YB = {
    __ready: true,
    BASE: APP_BASE,
    url: url,
    api: api,
    apiCached: apiCached,
    toast: toast,
    el: el,
    $: $,
    escapeHtml: escapeHtml,
    maskPhone: maskPhone,
    maskEmail: maskEmail,
    openModal: openModal,
    closeModal: closeModal,
    confirmDialog: confirmDialog,
    promptDialog: promptDialog,
    setTip: setTip,
    PW_CLASS_PATTERNS: PW_CLASS_PATTERNS,
    PW_MIN_LEN: PW_MIN_LEN,
    PW_MIN_CLASSES: PW_MIN_CLASSES,
    PW_POLICY_HINT: PW_POLICY_HINT,
    PW_ADMIN_MIN_LEN: PW_ADMIN_MIN_LEN,
    PW_ADMIN_MIN_CLASSES: PW_ADMIN_MIN_CLASSES,
    PW_ADMIN_HINT: PW_ADMIN_HINT,
    passwordClasses: passwordClasses,
    passwordPolicyOk: passwordPolicyOk,
    passwordPolicyOkAdmin: passwordPolicyOkAdmin,
    openPasswordModal: openPasswordModal,
    openConfirmPasswordModal: openConfirmPasswordModal,
    openPwModal: openPwModal,
    pwGateReason: pwGateReason,
    pwGateMessage: pwGateMessage,
    openDelayAckModal: openDelayAckModal,
    dangerousSubmit: dangerousSubmit,
    applyAnnouncementText: applyAnnouncementText,
    iconEl: iconEl,
    toggleTheme: toggleTheme,
    applyTheme: applyTheme,
    currentTheme: currentTheme,
    toggleDrawer: toggleDrawer,
    switchTab: switchTab,
    selectTab: selectTab,
    tabFromUrl: tabFromUrl,
    tabSyncUrl: tabSyncUrl,
    tabVisible: tabVisible,
    tabDeepLink: tabDeepLink,
    doLogout: doLogout,
    openChangelog: openChangelog,
    identity: identity,
    calibrateClock: calibrateClock,
    renderClock: renderClock,
    getServerNow: serverNow,
    clockString: clockString,
    clockInfo: clockInfo,
    setNavBadge: setNavBadge,
    loadNavBadges: loadNavBadges,
    SWAP_MS: SWAP_MS,
    swapOut: swapOut,
    prefs: {
      ownerEmailVisible: ownerEmailVisible,
      setOwnerEmailVisible: setOwnerEmailVisible
    }
  };
  window.YB = YB;
  // 兼容内联 onclick / 既有页面脚本引用的裸全局名
  window.api = api;
  window.toast = toast;
  window.$ = $;
  window.el = el;
  window.esc = escapeHtml;
  window.escapeHtml = escapeHtml;
  window.maskPhone = maskPhone;
  window.maskEmail = maskEmail;
  window.openModal = openModal;
  window.closeModal = closeModal;
  window.confirmDialog = confirmDialog;
  window.promptDialog = promptDialog;
  // 口令策略 / 密码模态的裸全局出口：classic 页面脚本（pages/*.js）按此名直呼
  window.PW_CLASS_PATTERNS = PW_CLASS_PATTERNS;
  window.PW_MIN_LEN = PW_MIN_LEN;
  window.PW_MIN_CLASSES = PW_MIN_CLASSES;
  window.PW_POLICY_HINT = PW_POLICY_HINT;
  window.PW_ADMIN_MIN_LEN = PW_ADMIN_MIN_LEN;
  window.PW_ADMIN_MIN_CLASSES = PW_ADMIN_MIN_CLASSES;
  window.PW_ADMIN_HINT = PW_ADMIN_HINT;
  window.passwordClasses = passwordClasses;
  window.passwordPolicyOk = passwordPolicyOk;
  window.passwordPolicyOkAdmin = passwordPolicyOkAdmin;
  window.openPasswordModal = openPasswordModal;
  window.openConfirmPasswordModal = openConfirmPasswordModal;
  window.openPwModal = openPwModal;
  window.openDelayAckModal = openDelayAckModal;
  window.dangerousSubmit = dangerousSubmit;
  window.toggleTheme = toggleTheme;
  window.toggleSidebar = toggleDrawer;
  window.switchTab = switchTab;
  window.doLogout = doLogout;
  window.openChangelog = openChangelog;
  window.calibrateClock = calibrateClock;
  window.getServerNow = serverNow;
})();
