// 项目交互层（Adminator 4.3.0 外壳）— 全局 api/toast/modal/时钟/身份/导航行为。
// 契约区间 L1-L245（tests/test_web_js_modules.py 钉住本文件与 pages/ 的加载顺序）。
// classic script（非 module）：依赖 partials/theme_boot.html 先行定义的全局 BASE。
(function () {
  "use strict";
  if (window.YB && window.YB.__ready) return; // base.html 与页面可能各引一次

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
    return fetch(url(req.path), { method: req.method, headers: headers, body: body, credentials: "same-origin" })
      .then(function (resp) {
        return resp.text().then(function (txt) { return handleResponse(resp, txt, req, retried); });
      }, function (err) { throw networkError(err); });
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
      return refreshThenRetry(req).catch(function () { throw httpError(403, "请刷新页面后重试"); });
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
  function api(method, path, body) {
    return perform(normalizeRequest(method, path, body), false);
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
  function toast(msg, isError) { return showToast(isError ? "error" : "info", msg); }
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
    var titleEl = el("div", { class: "modal-title", id: titleId, text: cfg.title || "" });
    head.appendChild(titleEl);
    var handle = {
      el: panel, panel: panel, backdrop: null, trigger: trigger,
      dismissible: cfg.dismissible !== false, onClose: cfg.onClose,
      titleEl: titleEl,
      close: function () { closeModal(handle); },
      setTitle: function (t) { titleEl.textContent = t; },
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
    $("#modal-host") ? $("#modal-host").appendChild(backdrop) : document.body.appendChild(backdrop);
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
  function toggleDropdown(trigger) {
    var wrap = trigger && trigger.closest ? trigger.closest(".dd-wrap") : null;
    if (!wrap) return;
    var willOpen = !wrap.classList.contains("is-open");
    closeDropdowns(wrap);
    wrap.classList.toggle("is-open", willOpen);
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
  function activateTab(group, target) {
    forEach(group.querySelectorAll(".tab[data-tab-target]"), function (t) {
      var on = t.getAttribute("data-tab-target") === target;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    forEach(group.querySelectorAll(".tab-panel[data-tab-id]"), function (p) {
      p.classList.toggle("is-active", p.getAttribute("data-tab-id") === target);
    });
  }
  function cssEscape(s) { return String(s).replace(/["\\]/g, "\\$&"); }
  function switchTab(name) {
    if (!name) return;
    var panel = document.querySelector('[data-tab-group] [data-tab-id="' + cssEscape(name) + '"]');
    if (panel) activateTab(panel.closest("[data-tab-group]"), name);
    forEach(document.querySelectorAll('[id^="tab-"]'), function (p) { p.classList.toggle("hidden", p.id !== "tab-" + name); });
    forEach(document.querySelectorAll("[data-tab-btn]"), function (b) { b.classList.toggle("is-active", b.getAttribute("data-tab-btn") === name); });
    try { document.dispatchEvent(new CustomEvent("yiban:tab", { detail: { name: name } })); } catch (e) {}
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
  function roleLabel(m) {
    if (!m) return "";
    if (m.is_builtin_admin) return "主管理员";
    return m.role === "admin" ? "管理员" : m.role === "user" ? "普通用户" : (m.role || "");
  }
  function hydrateIdentity() {
    return api("GET", "/api/me").then(function (data) {
      me = data;
      if (data && data.csrf_token) csrfToken = data.csrf_token;
      var name = data.username || data.email || "";
      setText("[data-account-name]", name + (data.is_builtin_admin ? "（主管理员）" : ""));
      setText("[data-account-email]", data.email || "");
      setText("[data-account-role]", roleLabel(data));
      var initial = String(data.username || data.email || "?").replace(/\s+/g, "").slice(0, 2).toUpperCase();
      setText("[data-account-avatar]", initial || "?");
      return data;
    }).catch(function () { return null; });
  }
  /* ---------- 服务器时钟 ---------- */
  var clock = { offset: 0, tz: 0, status: "", color: "" };
  function serverNow() {
    var epoch = Math.floor(Date.now() / 1000) + clock.offset + clock.tz * 60;
    return new Date(epoch * 1000);
  }
  function clockString() { return serverNow().toISOString().slice(0, 19).replace("T", " "); }
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
    return { now: clockString(), server_ts: Math.floor(serverNow().getTime() / 1000), tz_offset_min: clock.tz, sign_status: clock.status, color: clock.color };
  }
  function calibrateClock() {
    return api("GET", "/api/clock").then(function (data) {
      if (!data) return null;
      var ts = Number(data.server_ts);
      if (isFinite(ts)) clock.offset = ts - Math.floor(Date.now() / 1000);
      clock.tz = Number(data.tz_offset_min) || 0;
      clock.status = data.sign_status || "";
      clock.color = /^#[0-9a-f]{6}$/i.test(String(data.color || "")) ? data.color : "";
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
    api("GET", "/api/accounts").then(function (data) {
      var list = (data && data.accounts) || [];
      // 徽标口径与账号管理页「待处理账号」组一致：待审核 + 已拒绝。
      // 只数 pending 会让徽标数小于页面里的待处理条数，同一条目两处不一致。
      setNavBadge("accounts", list.filter(function (a) {
        return a && !a.deleted && (a.status === "pending" || a.status === "rejected");
      }).length);
    }).catch(function () {});
    api("GET", "/api/users").then(function (data) {
      var list = (data && data.users) || [];
      // 待处理用户 = 名下有「待审核或已拒绝」账号的用户数。
      // review_count 已是 pending+rejected 的超集，再叠加 pending_count 会重复计数。
      var review = list.filter(function (u) { return Number(u && u.review_count) > 0; }).length;
      setNavBadge("users", review);
    }).catch(function () {});
  }

  /* ---------- 公告 ---------- */
  function showAnnouncement(text) {
    openModal({ title: "公告", body: el("div", { class: "pm-announce", text: text }), actions: [{ label: "关闭", variant: "ghost" }] });
  }
  function initAnnouncement() {
    api("GET", "/api/announcement").then(function (data) {
      var text = String((data && data.text) || "").trim();
      if (!text) return;
      var btn = $("announcementBtn");
      if (btn) { btn.hidden = false; btn.addEventListener("click", function () { showAnnouncement(text); }); }
      var dot = document.querySelector("[data-announcement-dot]");
      if (dot) dot.hidden = false;
    }).catch(function () {});
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

    window.addEventListener("resize", function () { relayoutRail(); if (window.innerWidth > 720) toggleDrawer(false); });
    window.addEventListener("scroll", relayoutRail, true);
  }

  onReady(function () {
    initGlobalHandlers();
    var themeBtn = $("themeToggle");
    if (themeBtn) themeBtn.addEventListener("click", toggleTheme);
    updateThemeIcons();
    hydrateIdentity().then(function (identity) { if (identity) loadNavBadges(identity); });
    initAnnouncement();
    calibrateClock();
    renderClock();
    setInterval(renderClock, 1000);
    setInterval(calibrateClock, 60000);
  });

  /* ---------- 公开面 ---------- */
  var YB = {
    __ready: true,
    BASE: APP_BASE,
    url: url,
    api: api,
    toast: toast,
    el: el,
    $: $,
    escapeHtml: escapeHtml,
    openModal: openModal,
    closeModal: closeModal,
    confirmDialog: confirmDialog,
    promptDialog: promptDialog,
    toggleTheme: toggleTheme,
    applyTheme: applyTheme,
    currentTheme: currentTheme,
    toggleDrawer: toggleDrawer,
    switchTab: switchTab,
    doLogout: doLogout,
    openChangelog: openChangelog,
    calibrateClock: calibrateClock,
    renderClock: renderClock,
    getServerNow: serverNow,
    clockString: clockString,
    clockInfo: clockInfo,
    setNavBadge: setNavBadge,
    loadNavBadges: loadNavBadges
  };
  window.YB = YB;
  // 兼容内联 onclick / 既有页面脚本引用的裸全局名
  window.api = api;
  window.toast = toast;
  window.$ = $;
  window.el = el;
  window.esc = escapeHtml;
  window.escapeHtml = escapeHtml;
  window.openModal = openModal;
  window.closeModal = closeModal;
  window.confirmDialog = confirmDialog;
  window.promptDialog = promptDialog;
  window.toggleTheme = toggleTheme;
  window.toggleSidebar = toggleDrawer;
  window.switchTab = switchTab;
  window.doLogout = doLogout;
  window.openChangelog = openChangelog;
  window.calibrateClock = calibrateClock;
  window.getServerNow = serverNow;
})();
