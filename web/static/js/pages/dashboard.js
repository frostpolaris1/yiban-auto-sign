// 数据总览页脚本（classic script，非 module）。
// 依赖 base.html 先载入的 core.js（YB.api/el/toast/getServerNow）与本地 Chart.js 4.5.1。
// 所有图表颜色从 CSS 自定义属性读取，主题切换（document 的 yiban:theme 事件）时重建。
(function () {
  "use strict";
  var YB = window.YB || {};
  if (!YB.api) return;

  var REDUCED = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var charts = {};
  var state = { dailyMap: {}, dailyDays: [], byStatus: {}, calMonth: null, signLoaded: false, slots: [] };

  /* ---------------- 基础工具 ---------------- */
  function $(id) { return document.getElementById(id); }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function txt(node, value) { if (node) node.textContent = value == null ? "" : String(value); }
  function num(n) { n = Number(n); return isFinite(n) ? n.toLocaleString("zh-CN") : "0"; }
  function pctOf(a, b) { return b > 0 ? Math.round(a / b * 1000) / 10 : 0; }
  function el(tag, attrs, children) { return YB.el(tag, attrs, children); }

  function token(name) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v || "").trim();
  }
  function palette() {
    return {
      primary: token("--primary"), success: token("--success"), danger: token("--danger"),
      warning: token("--warning"), info: token("--info"), purple: token("--purple"),
      teal: token("--teal"), text: token("--t-base"), muted: token("--t-muted"),
      light: token("--t-light"), soft: token("--border-soft"), border: token("--border"),
      card: token("--bg-card")
    };
  }

  function serverDate() { return YB.getServerNow ? YB.getServerNow() : new Date(); }
  function pad2(n) { return ("0" + n).slice(-2); }
  function fmtDate(d) { return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()); }
  function fmtMonth(d) { return d.getFullYear() + "-" + pad2(d.getMonth() + 1); }
  function monthStart(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
  function todayStr() { return fmtDate(serverDate()); }
  function yesterdayStr() { var d = serverDate(); d.setDate(d.getDate() - 1); return fmtDate(d); }

  function skel(node, title) {
    if (!node) return; clear(node);
    node.appendChild(el("span", { class: "skeleton " + (title ? "skeleton--title" : "skeleton--text") + " dash-skel", "aria-hidden": "true" }));
  }
  function failNote(node, msg) {
    if (!node) return; clear(node);
    node.appendChild(el("span", { class: "dash-error-text", text: msg || "加载失败" }));
  }
  function setValue(node, value, sup) {
    if (!node) return; clear(node);
    node.classList.remove("kpi-value--empty");   // 真实数值回来时撤掉空态字号
    node.appendChild(document.createTextNode(String(value)));
    if (sup != null) node.appendChild(el("sup", { text: String(sup) }));
  }
  // 空态数值：不复用 44px 粗体（会把 "—" 渲染成一条 58×5 的黑横杠），改用专属字号/颜色
  function setEmptyValue(node, text) {
    if (!node) return; clear(node);
    node.classList.add("kpi-value--empty");
    txt(node, text || "—");
  }
  function setPill(node, text, cls) {
    if (!node) return;
    node.className = "kpi-pill " + (cls || "info");
    txt(node, text || "");
    node.hidden = !text;
  }
  function emptyNode(msg) {
    var box = el("div", { class: "empty" });
    var ico = el("span", { class: "empty__icon" });
    ico.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>';
    box.appendChild(ico);
    box.appendChild(el("div", { class: "empty__msg", text: msg || "暂无数据" }));
    return box;
  }
  function trendArrow(cls) {
    var paths = { up: "M7 17l10-10M7 7h10v10", down: "M7 7l10 10M7 17h10V7", flat: "M5 12h14" };
    var span = el("span", { class: "dash-trend" });
    span.innerHTML = '<svg class="' + cls + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="' + paths[cls] + '"/></svg>';
    return span;
  }

  /* ---------------- 状态词表（sign_events.status 的真实取值） ----------------
     取值与 scripts/signin.py 的 STATUS_* 常量一一对应，新增状态必须同步本表，
     否则分布图会把它当作「跳过」并显示英文原文。
     success/already=成功；failed=失败；其余（no_task/retrying/skipped_* 等）=
     跳过或未了结。探针 stage 同样写 success/failed，故本页请求显式带
     stage=sign，只统计真实签到（见 loadSign）。 */
  var SUCCESS_ST = { success: 1, already: 1 };
  var FAIL_ST = { failed: 1 };
  var STATUS_LABEL = {
    success: "成功", already: "已签到", no_task: "无需签到", failed: "失败",
    retrying: "重试中", skipped_window: "时段外跳过", skipped_norange: "窗口缺失",
    no_position: "无点位", paused: "账密暂停", user_cancelled: "用户取消",
    pending: "待签", global_paused: "全局暂停"
  };
  var STATUS_TOKEN = {
    success: "success", already: "success", failed: "danger", retrying: "warning",
    no_task: "info", no_position: "purple", paused: "warning",
    user_cancelled: "muted", skipped_window: "light", skipped_norange: "light",
    pending: "light", global_paused: "muted"
  };
  function statusKind(st) { st = String(st || ""); return SUCCESS_ST[st] ? "success" : (FAIL_ST[st] ? "fail" : "skip"); }
  function statusLabel(st) { return STATUS_LABEL[st] || String(st || "未知"); }
  function statusColor(st, t) { return t[STATUS_TOKEN[st] || "light"] || t.light; }

  /* ---------------- 图表宿主管理 ---------------- */
  function overlay(key, mode, msg) {
    var box = document.querySelector('[data-overlay="' + key + '"]');
    if (!box) return;
    if (mode === "none") { box.hidden = true; clear(box); return; }
    box.hidden = false; clear(box);
    if (mode === "loading") box.appendChild(el("span", { class: "spinner", "aria-hidden": "true" }));
    else if (mode === "empty") box.appendChild(emptyNode(msg));
    else box.appendChild(el("div", { class: "dash-error-text", text: msg || "加载失败" }));
  }
  function chartDefaults(t) {
    if (!window.Chart) return;
    Chart.defaults.font.family = "'Inter','Noto Sans SC',system-ui,sans-serif";
    Chart.defaults.font.size = 12;
    Chart.defaults.color = t.muted;
    Chart.defaults.borderColor = t.soft;
  }
  function baseOpts(t) {
    return {
      responsive: true, maintainAspectRatio: false,
      animation: REDUCED ? false : { duration: 400 },
      plugins: {
        legend: { position: "bottom", labels: { color: t.muted, usePointStyle: true, boxWidth: 8, boxHeight: 8, padding: 16 } },
        tooltip: {
          backgroundColor: t.text, titleColor: t.card, bodyColor: t.card,
          padding: 10, cornerRadius: 6, displayColors: true
        }
      }
    };
  }
  function draw(key, canvasId, config) {
    var canvas = $(canvasId);
    if (!canvas) return false;
    if (!window.Chart) { overlay(key, "error", "图表库未加载"); return false; }
    if (charts[key]) { try { charts[key].destroy(); } catch (e) { /* 忽略 */ } delete charts[key]; }
    if (!canvas.getClientRects().length) return false; // 容器隐藏时不建实例
    try {
      chartDefaults(palette());
      charts[key] = new Chart(canvas, config);
      return true;
    } catch (e) {
      overlay(key, "error", "图表渲染失败");
      return false;
    }
  }
  function renderMeta(id, cells) {
    var node = $(id + "-meta");
    if (!node) return;
    clear(node); node.hidden = false;
    cells.forEach(function (pair) {
      var cell = el("div", { class: "chart-meta-cell" });
      cell.appendChild(el("span", { class: "chart-meta-label", text: pair[0] }));
      cell.appendChild(el("span", { class: "chart-meta-value", text: pair[1] }));
      node.appendChild(cell);
    });
  }

  /* ---------------- KPI：容量 ---------------- */
  function loadSettings() {
    return YB.api("GET", "/api/settings").then(function (d) {
      renderCapacity((d && d.capacity) || null);
      renderPause(d || null);
    }).catch(function () {
      renderCapacity(null);
      renderPause(null);
    });
  }
  function renderCapacity(cap) {
    var av = $("kpi-accounts-value"), as = $("kpi-accounts-sub");
    var uv = $("kpi-users-value"), us = $("kpi-users-sub");
    if (!cap) {
      failNote(av, "—"); failNote(as, "容量读取失败");
      failNote(uv, "—"); failNote(us, "容量读取失败");
      setPill($("kpi-accounts-pill"), "", ""); setPill($("kpi-users-pill"), "", "");
      return;
    }
    var acc = Number(cap.accounts) || 0, amax = Number(cap.accounts_max) || 0;
    var bd = cap.accounts_breakdown || {};
    setValue(av, num(acc), amax > 0 ? "/" + num(amax) : null);
    txt(as, "正常 " + num(bd.normal) + " · 用户暂停 " + num(bd.user_paused) + " · 账密故障 " + num(bd.cred_paused) + "（均不含已删除）");
    setPill($("kpi-accounts-pill"), amax > 0 ? pctOf(acc, amax) + "%" : "未设上限", pctOf(acc, amax) >= 90 ? "down" : "info");

    var users = Number(cap.users) || 0, umax = Number(cap.users_max) || 0;
    setValue(uv, num(users), umax > 0 ? "/" + num(umax) : null);
    txt(us, umax > 0 ? "剩余注册名额 " + num(Math.max(0, umax - users)) + "（含未提交账号的空用户）" : "未设上限");
    setPill($("kpi-users-pill"), umax > 0 ? pctOf(users, umax) + "%" : "未设上限", pctOf(users, umax) >= 90 ? "down" : "info");
  }
  function pauseBadge(node, paused, labels) {
    if (!node) return; clear(node);
    node.appendChild(el("span", { class: "badge " + (paused ? "danger" : "success") + " dot", text: paused ? labels[0] : labels[1] }));
  }
  function renderPause(d) {
    if (!d) {
      failNote($("health-global-pause"), "读取失败");
      failNote($("health-reg-pause"), "读取失败");
      return;
    }
    pauseBadge($("health-global-pause"), !!Number(d.global_pause), ["全局暂停中", "正常运行"]);
    pauseBadge($("health-reg-pause"), !!Number(d.registration_pause), ["注册已暂停", "注册开放"]);
    // 周末开关决定「今日无结果」的解释，故设置先到即重算一次成功率文案。
    state.satSign = Number(d.saturday_sign) === 1;
    state.sunSign = Number(d.sunday_sign) === 1;
    state.weekendKnown = true;
    renderRateKpi();
  }

  /* ---------------- KPI + 图表：签到事件 ---------------- */
  function loadSign() {
    return YB.api("GET", "/api/admin/sign-events?days=30&stage=sign").then(function (d) {
      normalizeDaily((d && d.daily_stats) || []);
      state.signLoaded = true;
      var days = Number((d && d.days) || 30);
      txt($("trend-coverage"), "最近 " + days + " 天 · 仅真实签到");
      txt($("dist-coverage"), "最近 " + days + " 天 · 仅真实签到");
      renderRateKpi();
      renderTrend();
      renderDist();
      renderCalendar();
    }).catch(function (err) {
      state.signLoaded = false;
      var msg = (err && err.message) || "请求失败";
      overlay("trend", "error", "签到事件加载失败：" + msg);
      overlay("dist", "error", "签到事件加载失败：" + msg);
      failNote($("kpi-rate-value"), "—");
      failNote($("kpi-rate-sub"), "签到事件加载失败");
      setPill($("kpi-rate-pill"), "", "");
      renderCalendar();
      txt($("cal-note"), "签到事件加载失败：" + msg);
    });
  }
  function normalizeDaily(rows) {
    var map = {}, byStatus = {};
    rows.forEach(function (r) {
      var day = String((r && r.day) || "");
      if (!day) return;
      var cnt = Number(r.cnt) || 0, st = String(r.status || "");
      var m = map[day] || (map[day] = { success: 0, fail: 0, skip: 0, total: 0 });
      m.total += cnt; m[statusKind(st)] += cnt;
      byStatus[st] = (byStatus[st] || 0) + cnt;
    });
    state.dailyMap = map;
    state.dailyDays = Object.keys(map).sort();
    state.byStatus = byStatus;
  }
  function rateOf(m) {
    if (!m) return null;
    var att = m.success + m.fail;
    return att > 0 ? Math.round(m.success / att * 1000) / 10 : null;
  }
  // 无结果文案要区分「当天本来就不签到」与「还没产生结果」：
  // 周六/周日签到可在系统设置中关闭，此时显示「暂无结果」会误导管理员以为调度异常。
  function noResultText() {
    if (state.weekendKnown) {
      var dow = serverDate().getDay();
      if (dow === 6 && state.satSign === false) return "今日不签到（周六签到已关闭）";
      if (dow === 0 && state.sunSign === false) return "今日不签到（周日签到已关闭）";
    }
    return "今日暂无签到结果";
  }
  function renderRateKpi() {
    var today = todayStr(), m = state.dailyMap[today], ry = rateOf(state.dailyMap[yesterdayStr()]);
    var rt = rateOf(m), v = $("kpi-rate-value"), sub = $("kpi-rate-sub");
    clear(v);
    if (rt == null) {
      setEmptyValue(v, "—");
      v.title = "";
      clear(sub);
      sub.appendChild(el("span", { class: "dash-muted", text: noResultText() }));
      setPill($("kpi-rate-pill"), "", "");
      return;
    }
    v.appendChild(document.createTextNode(rt.toFixed(1)));
    v.appendChild(el("sup", { text: "%" }));
    // 口径说明放 tooltip：写进副文案会把卡片挤成多行；「较昨日」已由右上角药丸表达，
    // 副文案只保留结果构成，避免同一信息在卡内出现两次。
    v.title = "成功率 = 成功 ÷（成功 + 失败），跳过不计入";
    clear(sub);
    sub.appendChild(el("span", { text: "成功 " + num(m.success) + " · 失败 " + num(m.fail) + (m.skip > 0 ? " · 跳过 " + num(m.skip) : "") }));
    if (ry != null) {
      var diff = Math.round((rt - ry) * 10) / 10;
      var cls = diff > 0 ? "up" : diff < 0 ? "down" : "flat";
      var pill = $("kpi-rate-pill");
      setPill(pill, "较昨日 " + (diff > 0 ? "+" : "") + diff + "%", cls);
      if (pill) pill.title = "与昨日签到成功率的变化";
    } else {
      setPill($("kpi-rate-pill"), "无昨日对比", "flat");
    }
  }
  function renderTrend() {
    var days = state.dailyDays;
    if (!days.length) { overlay("trend", "empty", "最近 30 天无真实签到记录"); txt($("trend-coverage"), "无数据"); return; }
    var labels = [], ok = [], fail = [], skip = [], sum = { ok: 0, fail: 0, skip: 0 };
    days.forEach(function (day) {
      var m = state.dailyMap[day];
      labels.push(day.slice(5));
      ok.push(m.success); fail.push(m.fail); skip.push(m.skip);
      sum.ok += m.success; sum.fail += m.fail; sum.skip += m.skip;
    });
    var t = palette(), opt = baseOpts(t);
    opt.interaction = { mode: "index", intersect: false };
    opt.scales = {
      x: { stacked: true, ticks: { color: t.muted, autoSkip: true, maxRotation: 0, maxTicksLimit: 12 }, grid: { display: false }, border: { color: t.soft } },
      y: { stacked: true, beginAtZero: true, ticks: { color: t.muted, precision: 0, callback: function (v) { return num(v); } }, grid: { color: t.soft }, border: { display: false } }
    };
    opt.plugins.tooltip.callbacks = { label: function (c) { return c.dataset.label + "：" + num(c.parsed.y) + " 次"; } };
    opt.plugins.tooltip.mode = "index";
    opt.plugins.tooltip.intersect = false;
    var drawn = draw("trend", "chart-trend", {
      type: "bar",
      data: {
        labels: labels,
        datasets: [
          { label: "成功", data: ok, backgroundColor: t.success, stack: "sign", borderRadius: 3, barPercentage: 0.72 },
          { label: "失败", data: fail, backgroundColor: t.danger, stack: "sign", borderRadius: 3, barPercentage: 0.72 },
          { label: "跳过", data: skip, backgroundColor: t.warning, stack: "sign", borderRadius: 3, barPercentage: 0.72 }
        ]
      },
      options: opt
    });
    overlay("trend", drawn ? "none" : "error", "图表渲染失败");
    var total = sum.ok + sum.fail + sum.skip;
    renderMeta("trend", [
      ["近 30 天成功", num(sum.ok)],
      ["近 30 天失败", num(sum.fail)],
      ["近 30 天跳过", num(sum.skip)],
      ["日均签到事件", num(days.length ? Math.round(total / days.length * 10) / 10 : 0)]
    ]);
  }
  function renderDist() {
    var by = state.byStatus, keys = Object.keys(by).filter(function (k) { return by[k] > 0; });
    keys.sort(function (a, b) { return by[b] - by[a]; });
    if (!keys.length) { overlay("dist", "empty", "暂无签到结果数据"); return; }
    var t = palette(), total = keys.reduce(function (n, k) { return n + by[k]; }, 0);
    var opt = baseOpts(t);
    opt.cutout = "68%";
    opt.plugins.tooltip.callbacks = {
      label: function (c) {
        var v = Number(c.parsed) || 0;
        return c.label + "：" + num(v) + " 次（" + (total > 0 ? Math.round(v / total * 1000) / 10 : 0) + "%）";
      }
    };
    var drawn = draw("dist", "chart-dist", {
      type: "doughnut",
      data: {
        labels: keys.map(statusLabel),
        datasets: [{ data: keys.map(function (k) { return by[k]; }), backgroundColor: keys.map(function (k) { return statusColor(k, t); }), borderColor: t.card, borderWidth: 2 }]
      },
      options: opt
    });
    overlay("dist", drawn ? "none" : "error", "图表渲染失败");
    renderMeta("dist", [["签到事件总数", num(total)], ["结果类型", num(keys.length)]]);
  }
  function renderCalendar() {
    var grid = $("mini-cal");
    if (!grid) return;
    clear(grid);
    var today = todayStr();
    var base = state.calMonth ? new Date(state.calMonth + "-01T00:00:00") : monthStart(serverDate());
    ["一", "二", "三", "四", "五", "六", "日"].forEach(function (wd) {
      grid.appendChild(el("div", { class: "mini-cal-wd", text: wd }));
    });
    var y = base.getFullYear(), mo = base.getMonth();
    var lead = (new Date(y, mo, 1).getDay() + 6) % 7; // 周一起始
    var daysIn = new Date(y, mo + 1, 0).getDate();
    var trail = (7 - ((lead + daysIn) % 7)) % 7;
    var i;
    for (i = 0; i < lead; i++) grid.appendChild(el("div", { class: "mini-cal-day is-other", "aria-hidden": "true" }));
    for (i = 1; i <= daysIn; i++) {
      var ds = fmtDate(new Date(y, mo, i));
      var m = state.dailyMap[ds];
      var cls = "mini-cal-day";
      if (ds > today) cls += " is-future";
      else if (m && m.total > 0) cls += m.fail > 0 ? " is-fail" : (m.success > 0 ? " is-ok" : " is-none");
      else cls += " is-none";
      if (ds === today) cls += " is-today";
      var cell = el("div", { class: cls, text: String(i), role: "gridcell" });
      cell.title = (m && m.total > 0)
        ? "成功 " + m.success + " · 失败 " + m.fail + " · 跳过 " + m.skip
        : (ds > today ? "未来日期（尚未签到）" : "当日无真实签到记录");
      grid.appendChild(cell);
    }
    for (i = 0; i < trail; i++) grid.appendChild(el("div", { class: "mini-cal-day is-other", "aria-hidden": "true" }));
    txt($("cal-label"), y + " 年 " + (mo + 1) + " 月");
  }
  function shiftMonth(delta) {
    var base = state.calMonth ? new Date(state.calMonth + "-01T00:00:00") : monthStart(serverDate());
    base.setMonth(base.getMonth() + delta);
    state.calMonth = fmtMonth(base);
    renderCalendar();
  }

  /* ---------------- 自选时间片 ---------------- */
  function loadSlots() {
    return YB.api("GET", "/api/time-prefs/stats").then(function (d) {
      var slots = ((d && d.slots) || []).filter(function (s) { return s && !s.disabled; });
      state.slots = slots;
      renderSlots(slots);
    }).catch(function (err) {
      overlay("slots", "error", "时间片数据加载失败：" + ((err && err.message) || "请求失败"));
    });
  }
  function renderSlots(slots) {
    if (!slots.length) { overlay("slots", "empty", "当前无可用的自选时间片"); txt($("slots-coverage"), "无数据"); return; }
    var t = palette(), opt = baseOpts(t);
    opt.indexAxis = "y";
    opt.scales = {
      x: { beginAtZero: true, ticks: { color: t.muted, precision: 0, callback: function (v) { return num(v); } }, grid: { color: t.soft }, border: { display: false } },
      y: { ticks: { color: t.muted }, grid: { display: false }, border: { color: t.soft } }
    };
    opt.plugins.tooltip.callbacks = { label: function (c) { return c.dataset.label + "：" + num(c.parsed.x) + " 人"; } };
    var drawn = draw("slots", "chart-slots", {
      type: "bar",
      data: {
        labels: slots.map(function (s) { return String(s.label || ""); }),
        datasets: [
          { label: "自选人数", data: slots.map(function (s) { return Number(s.count) || 0; }), backgroundColor: t.primary, borderRadius: 4, barPercentage: 0.72 },
          { label: "该时段人数上限", data: slots.map(function (s) { return Number(s.cap) || 0; }), backgroundColor: t.soft, borderRadius: 4, barPercentage: 0.72 }
        ]
      },
      options: opt
    });
    overlay("slots", drawn ? "none" : "error", "图表渲染失败");
    var total = slots.reduce(function (n, s) { return n + (Number(s.count) || 0); }, 0);
    var top = slots.slice().sort(function (a, b) { return (Number(b.count) || 0) - (Number(a.count) || 0); })[0];
    txt($("slots-coverage"), "共 " + slots.length + " 个可选时段");
    renderMeta("slots", [
      ["自选人数合计", num(total)],
      ["最热门时段", top ? String(top.label || "") + "（" + num(top.count) + " 人）" : "—"]
    ]);
  }

  /* ---------------- KPI：待处理事项 ---------------- */
  function loadPending() {
    // 待处理账号口径 = 账号管理「待处理账号」组：待审核 + 已拒绝，单一口径不叠加。
    // 待处理用户 = 名下有上述账号的用户数（/api/users 的 review_count 正是该口径）。
    // 不可把 review_count 与 pending_count 相加：review_count 已是 pending+rejected 的
    // 超集，相加会把同一批待审核账号算两遍。
    var pendingAccounts = YB.api("GET", "/api/accounts").then(function (d) {
      var list = (d && d.accounts) || [];
      return list.filter(function (a) {
        return a && !a.deleted && (a.status === "pending" || a.status === "rejected");
      }).length;
    }).catch(function () { return null; });
    var pendingUsers = YB.api("GET", "/api/users").then(function (d) {
      var list = (d && d.users) || [];
      return list.filter(function (u) { return Number(u && u.review_count) > 0; }).length;
    }).catch(function () { return null; });
    Promise.all([pendingAccounts, pendingUsers]).then(function (res) {
      var a = res[0], u = res[1], v = $("kpi-pending-value"), sub = $("kpi-pending-sub");
      if (a === null && u === null) { failNote(v, "—"); failNote(sub, "待处理数据加载失败"); return; }
      a = a || 0; u = u || 0;
      setValue(v, num(a + u), null);
      txt(sub, "待处理账号 " + num(a) + " · 待处理用户 " + num(u) + "（账号含已拒绝）");
    });
  }

  /* ---------------- 系统状态 ---------------- */
  function loadHealth() {
    YB.api("GET", "/api/clock").then(renderClock).catch(function () { failNote($("health-clock"), "时间校准失败"); });
    YB.api("GET", "/api/announcement").then(renderAnnouncement).catch(function () { failNote($("health-announcement"), "公告加载失败"); });
  }
  function renderClock(d) {
    var node = $("health-clock");
    if (!node) return;
    clear(node);
    if (!d || d.server_ts == null) { failNote(node, "时间校准失败"); return; }
    var drift = Math.round(Number(d.server_ts) - Date.now() / 1000);
    var abs = Math.abs(drift);
    var cls = abs <= 5 ? "success" : abs <= 60 ? "warning" : "danger";
    node.appendChild(el("span", { class: "badge " + cls + " dot", text: abs <= 5 ? "时钟已同步" : "偏差 " + (drift > 0 ? "+" : "") + drift + " 秒" }));
    var tz = Number(d.tz_offset_min) || 0;
    node.appendChild(el("span", { class: "dash-health-text", text: String(d.now || "") + "（UTC" + (tz >= 0 ? "+" : "") + (tz / 60) + "）" }));
  }
  function renderAnnouncement(d) {
    var node = $("health-announcement");
    if (!node) return;
    clear(node);
    var text = String((d && d.text) || "").trim();
    if (!text) { node.appendChild(el("span", { class: "dash-muted", text: "暂无公告" })); return; }
    node.appendChild(el("span", { class: "dash-health-text", text: text }));
  }
  function doPing() {
    var btn = $("ping-btn"), out = $("health-ping");
    if (!out) return;
    if (btn) btn.disabled = true;
    clear(out);
    out.appendChild(el("span", { class: "spinner sm", "aria-hidden": "true" }));
    YB.api("POST", "/api/ping").then(function (d) {
      clear(out);
      var ok = !!(d && d.reachable);
      out.appendChild(el("span", { class: "badge " + (ok ? "success" : "danger") + " dot", text: ok ? "可达" : "不可达" }));
      if (d && d.detail) out.appendChild(el("span", { class: "dash-health-text", text: String(d.detail) }));
    }).catch(function (err) {
      clear(out);
      out.appendChild(el("span", { class: "badge danger dot", text: "检测失败" }));
      out.appendChild(el("span", { class: "dash-health-text", text: (err && err.message) || "请求失败" }));
    }).then(function () { if (btn) btn.disabled = false; });
  }

  /* ---------------- 主题切换：重建读取 CSS 变量的图表 ---------------- */
  document.addEventListener("yiban:theme", function () {
    if (state.signLoaded && state.dailyDays.length) { renderTrend(); renderDist(); }
    if (state.slots.length) renderSlots(state.slots);
  });

  function init() {
    state.calMonth = fmtMonth(serverDate());
    renderCalendar();
    var prev = $("cal-prev"), next = $("cal-next"), ping = $("ping-btn");
    if (prev) prev.addEventListener("click", function () { shiftMonth(-1); });
    if (next) next.addEventListener("click", function () { shiftMonth(1); });
    if (ping) ping.addEventListener("click", doPing);
    loadSettings(); loadSign(); loadSlots(); loadPending(); loadHealth();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
