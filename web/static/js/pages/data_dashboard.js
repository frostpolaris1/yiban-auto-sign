// 数据总览页脚本（classic script，非 module）。
// 依赖 base.html 先载入的 core.js（YB.api/el/toast/getServerNow）与本地 Chart.js 4.5.1。
// 所有图表颜色从 CSS 自定义属性读取，主题切换（document 的 yiban:theme 事件）时重建。
(function () {
  "use strict";
  var YB = window.YB || {};
  if (!YB.api) return;

  var REDUCED = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var charts = {};
  var state = { dailyMap: {}, dailyDays: [], byStatus: {}, calMonth: null, signLoaded: false, signFailed: false, slots: [] };

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
  // 热力图脚注的初始文案（模板里带口径说明）：签到事件加载失败会改写它，重试成功后按此还原
  var CAL_NOTE0 = "";

  function skel(node, title) {
    if (!node) return; clear(node);
    node.appendChild(el("span", { class: "skeleton " + (title ? "skeleton--title" : "skeleton--text") + " dash-skel", "aria-hidden": "true" }));
  }
  function failNote(node, msg) {
    if (!node) return; clear(node);
    node.appendChild(el("span", { class: "dash-error-text", text: msg || "加载失败" }));
  }
  // KPI 副文案统一走这里：内容包一层 span 收敛为单行（超宽省略，见 app.css 第 28 节），
  // 四卡说明盒高度才能严格相等；完整口径（含从副文案删掉的括号注）进 title 兜底。
  function setSub(node, text, title, cls) {
    if (!node) return; clear(node);
    node.title = title || "";
    node.appendChild(el("span", { class: "kpi-compare-text" + (cls ? " " + cls : ""), text: text == null ? "" : String(text) }));
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
      renderCapacityRows(d || null);
      renderPause(d || null);
    }).catch(function (err) {
      // 卡内错误行先落位（复用各 render 的失败分支），再重抛给页级状态条计数
      renderCapacity(null);
      renderCapacityRows(null);
      renderPause(null);
      throw err;
    });
  }
  /* ---------------- 容量卡（原在账号管理页，并入总览后管理端只此一处） ---------------- */
  function capacityText(cur, max, breakdown) {
    var used = String(cur);
    if (!(Number(max) > 0)) return used + " / 不限";
    var parts = [];
    if (breakdown) {
      parts.push("正常 " + (breakdown.normal || 0));
      parts.push("用户暂停 " + (breakdown.user_paused || 0));
      parts.push("账密暂停 " + (breakdown.cred_paused || 0));
    }
    return used + " / " + max + (parts.length ? "（" + parts.join(" · ") + "）" : "");
  }
  function renderCapacityRows(d) {
    var acc = $("capacity-accounts"), usr = $("capacity-users"), est = $("capacity-estimate");
    if (!acc || !usr || !est) return;
    if (!d) {
      failNote(acc, "加载失败"); failNote(usr, "加载失败"); failNote(est, "加载失败");
      return;
    }
    var c = d.capacity || {};
    txt(acc, capacityText(c.accounts, c.accounts_max, c.accounts_breakdown));
    txt(usr, capacityText(c.users, c.users_max, null));
    var e = d.capacity_estimate || {};
    txt(est, "容量 " + (e.accounts_cap != null ? e.accounts_cap : "—") +
      " · 当前 " + (e.current_accounts != null ? e.current_accounts : "—") +
      " · 潜在 " + (e.potential_load != null ? e.potential_load : "—"));
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
    // 「（均不含已删除）」括号注删出副文案（行数对齐），口径挪进 title 提示
    setSub(as, "正常 " + num(bd.normal) + " · 用户暂停 " + num(bd.user_paused) + " · 账密故障 " + num(bd.cred_paused), "均不含已删除账号");
    setPill($("kpi-accounts-pill"), amax > 0 ? pctOf(acc, amax) + "%" : "未设上限", pctOf(acc, amax) >= 90 ? "down" : "info");

    var users = Number(cap.users) || 0, umax = Number(cap.users_max) || 0;
    setValue(uv, num(users), umax > 0 ? "/" + num(umax) : null);
    // 「（含未提交账号的空用户）」同理：只在有名额数字时才相关，随名额一起进 title
    if (umax > 0) setSub(us, "剩余注册名额 " + num(Math.max(0, umax - users)), "按含未提交账号的空用户计");
    else setSub(us, "未设上限", "");
    setPill($("kpi-users-pill"), umax > 0 ? pctOf(users, umax) + "%" : "未设上限", pctOf(users, umax) >= 90 ? "down" : "info");
  }
  function pauseBadge(node, paused, labels) {
    if (!node) return; clear(node);
    // 徽标用 .badge--* AA 档位：暂停=bad（负面）、运行=ok
    node.appendChild(el("span", { class: "badge " + (paused ? "badge--bad" : "badge--ok") + " dot", text: paused ? labels[0] : labels[1] }));
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
      state.signFailed = false;
      var days = Number((d && d.days) || 30);
      txt($("trend-coverage"), "最近 " + days + " 天 · 仅真实签到");
      txt($("dist-coverage"), "最近 " + days + " 天 · 仅真实签到");
      txt($("cal-note"), CAL_NOTE0);   // 还原上次失败改写的脚注
      renderRateKpi();
      renderTrend();
      renderDist();
      renderCalendar();
    }).catch(function (err) {
      state.signLoaded = false;
      state.signFailed = true;
      var msg = (err && err.message) || "请求失败";
      overlay("trend", "error", "签到事件加载失败：" + msg);
      overlay("dist", "error", "签到事件加载失败：" + msg);
      failNote($("kpi-rate-value"), "—");
      failNote($("kpi-rate-sub"), "签到事件加载失败");
      setPill($("kpi-rate-pill"), "", "");
      renderCalendar();
      txt($("cal-note"), "签到事件加载失败：" + msg);
      throw err;   // 重抛给页级状态条计数
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
    // 签到事件失败时本卡保持 failNote 错误行：设置先到会触发这里的重算，
    // 不能让「今日暂无签到结果」把「签到事件加载失败」盖掉（失败 ≠ 没有结果）。
    if (state.signFailed) return;
    var today = todayStr(), m = state.dailyMap[today], ry = rateOf(state.dailyMap[yesterdayStr()]);
    var rt = rateOf(m), v = $("kpi-rate-value"), sub = $("kpi-rate-sub");
    clear(v);
    if (rt == null) {
      setEmptyValue(v, "—");
      v.title = "";
      setSub(sub, noResultText(), "", "dash-muted");
      setPill($("kpi-rate-pill"), "", "");
      return;
    }
    v.appendChild(document.createTextNode(rt.toFixed(1)));
    v.appendChild(el("sup", { text: "%" }));
    // 口径说明放 tooltip：写进副文案会把卡片挤成多行；「较昨日」已由右上角药丸表达，
    // 副文案只保留结果构成，避免同一信息在卡内出现两次。
    v.title = "成功率 = 成功 ÷（成功 + 失败），跳过不计入";
    setSub(sub, "成功 " + num(m.success) + " · 失败 " + num(m.fail) + (m.skip > 0 ? " · 跳过 " + num(m.skip) : ""));
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
      throw err;   // 重抛给页级状态条计数
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

  /* ---------------- KPI：待处理账号 ---------------- */
  function loadPending() {
    // 口径与账号管理「待处理账号」组、侧栏徽标完全一致：待审核 + 已拒绝，单一口径不叠加。
    // 「名下有此类账号的用户数」不再上屏：全量数据下它与账号数相等，读起来是同一件事说两遍。
    function setAlert(active) {
      var v = $("kpi-pending-value");
      var card = v && v.closest(".kpi-card");
      if (card) card.classList.toggle("is-alert", !!active);
    }
    return YB.api("GET", "/api/accounts").then(function (d) {
      var v = $("kpi-pending-value"), sub = $("kpi-pending-sub");
      var list = (d && d.accounts) || [];
      var pending = 0, rejected = 0;
      list.forEach(function (a) {
        if (!a || a.deleted) return;
        if (a.status === "pending") pending += 1;
        else if (a.status === "rejected") rejected += 1;
      });
      var total = pending + rejected;
      setValue(v, num(total), null);
      setSub(sub, "待审核 " + num(pending) + " · 已拒绝 " + num(rejected));
      // 有可处置项时才把这张卡升级为唯一强调；0 或失败保持中性
      setAlert(total > 0);
    }).catch(function (err) {
      setAlert(false);
      failNote($("kpi-pending-value"), "—");
      failNote($("kpi-pending-sub"), "待处理账号加载失败");
      throw err;   // 重抛给页级状态条计数
    });
  }

  /* ---------------- 系统状态 ---------------- */
  function loadHealth() {
    var clock = YB.api("GET", "/api/clock").then(renderClock).catch(function (err) {
      failNote($("health-clock"), "时间校准失败");
      throw err;   // 重抛给页级状态条计数
    });
    var ann = YB.api("GET", "/api/announcement").then(renderAnnouncement).catch(function (err) {
      failNote($("health-announcement"), "公告加载失败");
      throw err;
    });
    return Promise.all([clock, ann]);
  }
  function renderClock(d) {
    var node = $("health-clock");
    if (!node) return;
    clear(node);
    if (!d || d.server_ts == null) { failNote(node, "时间校准失败"); return; }
    var drift = Math.round(Number(d.server_ts) - Date.now() / 1000);
    var abs = Math.abs(drift);
    // 徽标用 .badge--* AA 档位：同步=ok、小偏差=warn、大偏差=bad
    var cls = abs <= 5 ? "badge--ok" : abs <= 60 ? "badge--warn" : "badge--bad";
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
      out.appendChild(el("span", { class: "badge " + (ok ? "badge--ok" : "badge--bad") + " dot", text: ok ? "可达" : "不可达" }));
      if (d && d.detail) out.appendChild(el("span", { class: "dash-health-text", text: String(d.detail) }));
    }).catch(function (err) {
      clear(out);
      out.appendChild(el("span", { class: "badge badge--bad dot", text: "检测失败" }));
      out.appendChild(el("span", { class: "dash-health-text", text: (err && err.message) || "请求失败" }));
    }).then(function () { if (btn) btn.disabled = false; });
  }

  /* ---------------- 页级加载状态（部分卡失败时的统一重试出口） ----------------
     总览 8 个数据点分属 6 张卡，卡内已有轻量错误行（dash-error-text / 图表 overlay），
     逐卡再放重试按钮会喧宾夺主：页级一条状态条汇总失败并重跑全部加载点（与 work_users
     的状态条重试同一形态）。首屏不显示加载中 —— 卡片骨架已表达。 */
  function setStatus(tone, text, retry) {
    var box = $("dash-status");
    if (!box) return;
    box.classList.remove("info", "danger");
    box.classList.add(tone);
    txt($("dash-status-text"), text);
    var btn = $("dash-retry-btn");
    if (btn) btn.hidden = !retry;
    box.hidden = false;
  }
  function hideStatus() { var box = $("dash-status"); if (box) box.hidden = true; }
  // 各 load* 内部已渲染卡内错误行并把错误重抛上来；这里只按「是否全成功」收放状态条。
  function settled(p) { return p.then(function () { return true; }, function () { return false; }); }
  function loadAll() {
    var tasks = [loadSettings(), loadSign(), loadSlots(), loadPending(), loadHealth()];
    return Promise.all(tasks.map(settled)).then(function (rs) {
      var fails = rs.filter(function (ok) { return !ok; }).length;
      if (fails > 0) setStatus("danger", "部分数据加载失败（" + fails + " 项），卡片内已标注", true);
      else hideStatus();
    });
  }
  function retryAll() {
    setStatus("info", "正在重新加载数据…", false);
    loadAll();
  }

  /* ---------------- 主题切换：重建读取 CSS 变量的图表 ---------------- */
  document.addEventListener("yiban:theme", function () {
    if (state.signLoaded && state.dailyDays.length) { renderTrend(); renderDist(); }
    if (state.slots.length) renderSlots(state.slots);
  });

  function init() {
    state.calMonth = fmtMonth(serverDate());
    var noteNode = $("cal-note");
    CAL_NOTE0 = noteNode ? noteNode.textContent : "";
    renderCalendar();
    var prev = $("cal-prev"), next = $("cal-next"), ping = $("ping-btn"), retry = $("dash-retry-btn");
    if (prev) prev.addEventListener("click", function () { shiftMonth(-1); });
    if (next) next.addEventListener("click", function () { shiftMonth(1); });
    if (ping) ping.addEventListener("click", doPing);
    if (retry) retry.addEventListener("click", retryAll);
    loadAll();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
