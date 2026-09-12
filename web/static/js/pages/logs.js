/* 签到日志页（管理端 /logs）行为。
   依赖 core.js（YB.api/el/$/toast/identity/maskPhone）与共享组件
   components/account-ops.js（手动签到的「详情取号 → 提交」链路）。

   职责：手动签到下拉（只列生效未删账号）→ 日志检索/显示全部/导出/按日期查看/自动刷新
   → 探针与签到事件的结构化时间线；页面可见且今天视图时 10s 静默轮询。

   脱敏：列表与日志行内的手机号均经 YB.maskPhone 幂等脱敏；手动签到的完整号只在
   account-ops 的请求体里流转，不写入 DOM。所有后端数据一律经 YB.el/textContent 写入。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;
  var AUTO_KEY = "yiban-logs-autorefresh";

  // 状态码 → [中文标签, 色调]（未知状态回落原始码 + muted，保证信息不丢）
  var SIGN_MAP = {
    success: ["成功", "ok"], already: ["已签到", "ok"], no_task: ["无需签到", "muted"],
    failed: ["失败", "bad"], retrying: ["重试中", "warn"], pending: ["待签", "info"],
    skipped_window: ["超出时段", "warn"], skipped_norange: ["不在范围", "warn"],
    paused: ["已暂停", "muted"], user_cancelled: ["已取消", "muted"]
  };
  // 探针只有 failed / 其它 两态：非 failed 一律「正常」（与旧版 ✅/❌ 口径一致），
  // __default 兜住未来可能新增的非失败状态；原始状态码始终保留在 title，信息不丢。
  var PROBE_MAP = { failed: ["异常", "bad"], __default: ["正常", "ok"] };
  var state = {
    accounts: [], viewDate: "", search: "", all: false, autoRefresh: true,
    curDate: "", busy: false, exporting: false, firstLoad: true, snap: ""
  };

  /* ---------------- 小工具 ---------------- */
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function setText(id, value) { var n = $(id); if (n) n.textContent = value == null ? "" : String(value); }
  function resetSnap() { state.snap = ""; state.firstLoad = true; }
  function maskLine(line) {
    return String(line).replace(/\[(\d{11})\]/g, function (m, p) { return "[" + YB.maskPhone(p) + "]"; });
  }
  function isValidDate(s) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
    // 注意：不能用 `new Date(s + "T00:00:00").toISOString().slice(0,10) === s` 回环校验——
    // 字符串按**本地时区**解析，toISOString 却转 UTC；UTC+8 下会回退一天，
    // 所有日期都被判非法（实测 Asia/Shanghai 下 "2026-09-11" → "2026-09-10"），
    // 导致「查看」按钮与 ?date= 分享 URL 双双失效。改成按本地年月日构造再逐项比对。
    var y = +s.slice(0, 4), m = +s.slice(5, 7), d = +s.slice(8, 10);
    var dt = new Date(y, m - 1, d);
    return dt.getFullYear() === y && dt.getMonth() === m - 1 && dt.getDate() === d;
  }
  function readUrlDate() {
    var v = "";
    try { v = new URLSearchParams(location.search).get("date") || ""; } catch (e) { v = ""; }
    return isValidDate(v) ? v : "";
  }
  function writeUrlDate(date) {
    try {
      var u = new URL(location.href);
      if (date) u.searchParams.set("date", date); else u.searchParams.delete("date");
      history.replaceState(null, "", u.pathname + u.search + u.hash);  // 不 pushState：不堆历史
    } catch (e) { /* 无 history/URL 的环境静默降级 */ }
  }
  function findAccount(idx) {
    return state.accounts.filter(function (a) { return a && a.index === idx; })[0];
  }

  /* ---------------- 日志渲染 ---------------- */
  function infoText(data) {
    if (state.search) return data.returned + " 行匹配 / 共 " + data.total_lines + " 行";
    if (data.truncated) return "已截断：显示前 " + data.returned + " / 共 " + data.total_lines + " 行（导出可取完整文件）";
    if (data.total_lines > 80) return "共 " + data.total_lines + " 行（默认显示最后 80 行，可显示全部或导出）";
    return "共 " + data.total_lines + " 行";
  }

  function evRow(ev, map, isSign) {
    var li = YB.el("li", { class: "ev-row" });
    li.appendChild(YB.el("span", { class: "ev-time", text: ev.time || "--:--:--" }));
    li.appendChild(YB.el("span", { class: "ev-phone", text: YB.maskPhone(ev.phone || "") }));
    var mapped = map[ev.status] || map.__default;
    var label = mapped ? mapped[0] : (ev.status || "未知");
    li.appendChild(YB.el("span", {
      class: "badge badge--" + (mapped ? mapped[1] : "muted"),
      text: label, title: ev.status || ""
    }));
    var msg = String(ev.message || "");
    if (isSign && Number(ev.attempt) > 1) {
      msg = msg ? msg + "（第 " + ev.attempt + " 次）" : "（第 " + ev.attempt + " 次）";
    }
    if (msg) li.appendChild(YB.el("span", { class: "ev-msg", text: msg }));
    return li;
  }

  function renderEvents(listId, emptyId, countId, events, map, isSign) {
    var list = $(listId), empty = $(emptyId), count = $(countId);
    if (!list) return;
    clear(list);
    var arr = events || [];
    arr.forEach(function (ev) { list.appendChild(evRow(ev, map, isSign)); });
    if (count) count.textContent = String(arr.length);
    if (empty) empty.hidden = arr.length > 0;
  }

  function renderEmpty(empty, msg) {
    if (!empty) return;
    var node = empty.querySelector(".empty__msg");
    if (node) node.textContent = msg;
    empty.hidden = false;
  }

  function loadLogs(silent) {
    var params = new URLSearchParams();
    if (state.viewDate) params.set("date", state.viewDate);
    if (state.search) params.set("q", state.search);
    if (state.all) params.set("all", "1");
    var qs = params.toString();
    return YB.api("GET", "/api/logs" + (qs ? "?" + qs : "")).then(function (data) {
      if (!data) return;
      state.curDate = data.date || "";
      var info = infoText(data);
      var logs = data.logs || [];
      var rendered = logs.map(maskLine).join("\n");
      var probe = data.probe_events || [];
      var signev = data.sign_events || [];
      var hist = !!(state.viewDate || !data.is_today);
      // 快照含日志正文 + 探针 + 事件 + 行数信息 + 文件名/日期/历史标记：
      // 缺任何一项都会让"不同日期内容相同"时跳过重渲染，日期提示与文件名停在旧值。
      var snap = [rendered, JSON.stringify(probe), JSON.stringify(signev),
        info, data.log_file || "", data.date || "", hist ? "1" : "0"].join("\u0001");
      if (snap === state.snap) return;
      state.snap = snap;

      setText("log-info", info);
      setText("log-file", data.log_file || "");
      if (hist) {
        setText("log-date-tip", "正在查看 " + data.date + " 的日志（历史日期不自动刷新）");
      } else {
        setText("log-date-tip", "");
      }
      var todayBtn = $("log-today-btn");
      if (todayBtn) todayBtn.hidden = !hist;

      var box = $("log-box"), empty = $("log-empty");
      if (logs.length) {
        var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        box.textContent = rendered;
        box.hidden = false;
        if (empty) empty.hidden = true;
        // 沿用旧口径：仅"接近底部"或首次加载才自动滚到底，避免打断向上翻阅
        if (nearBottom || state.firstLoad) box.scrollTop = box.scrollHeight;
      } else {
        box.hidden = true;
        renderEmpty(empty, state.search ? "（无匹配日志行）"
          : (state.viewDate ? "（" + state.viewDate + " 无签到日志）" : "（暂无签到日志，等待定时任务执行…）"));
      }
      state.firstLoad = false;
      renderEvents("probe-list", "probe-empty", "probe-count", probe, PROBE_MAP, false);
      renderEvents("signev-list", "signev-empty", "signev-count", signev, SIGN_MAP, true);
    }).catch(function (e) {
      var msg = (e && e.message) || "日志加载失败，请稍后重试";
      if (silent) return;
      YB.toast.error(msg);
      // 首载失败时卡内不能停在空白框 + 「正在加载日志…」：给可读错误态。
      // 已有成功加载过（curDate 非空）时保留旧内容，避免一次抖动清空可读日志。
      if (!state.curDate) {
        setText("log-info", "日志加载失败");
        var box = $("log-box");
        if (box) box.hidden = true;
        renderEmpty($("log-empty"), msg);
      }
    });
  }

  /* ---------------- 账号下拉与手动签到 ---------------- */
  function syncSigninBtn() {
    var sel = $("signin-select"), btn = $("signin-btn");
    if (btn) btn.disabled = state.busy || !(sel && sel.value);
  }

  function fillSigninSelect() {
    var sel = $("signin-select");
    if (!sel) return;
    var current = sel.value;
    clear(sel);
    var signable = state.accounts.filter(function (a) {
      return a && a.status === "active" && !a.deleted;
    });
    if (!signable.length) {
      sel.appendChild(YB.el("option", { value: "", text: "暂无签到账号" }));
    } else {
      signable.forEach(function (a) {
        sel.appendChild(YB.el("option", {
          value: String(a.index),
          text: (a.display_name || ("账号" + a.index)) + " (" + YB.maskPhone(a.phone) + ")"
        }));
      });
      if (current) sel.value = current;
    }
    syncSigninBtn();
  }

  function loadAccounts() {
    return YB.api("GET", "/api/accounts").then(function (data) {
      state.accounts = (data && data.accounts) || [];
      fillSigninSelect();
    }).catch(function () { /* 下拉失败静默：日志仍可用 */ });
  }

  function refreshAll() { loadAccounts(); loadLogs(); }

  // 手动签到走共享组件：内部按需取详情拿完整手机号，完整号只在请求体里流转。
  var ops = YB.accountOps.create({
    busy: function (on) {
      var was = state.busy;
      state.busy = on;
      syncSigninBtn();
      if (was && !on) refreshAll();   // 请求结束后刷新下拉与日志（结果约 30 秒后落盘）
    },
    refresh: refreshAll
  });

  function doSignin() {
    var sel = $("signin-select");
    var acc = sel && sel.value ? findAccount(Number(sel.value)) : null;
    if (!acc) { YB.toast.error("请先选择要签到的账号"); return; }
    ops.signin(acc);
  }

  /* ---------------- 导出（fetch 下载，429/404 可提示） ---------------- */
  function exportLogs() {
    if (state.exporting) return;
    var date = state.viewDate || state.curDate;
    if (!date) { YB.toast.error("暂无可导出的日志日期"); return; }
    var btn = $("export-btn");
    state.exporting = true;
    if (btn) btn.disabled = true;
    fetch(YB.BASE + "/api/logs/export?date=" + encodeURIComponent(date), { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (txt) {
            var msg = "导出失败（" + resp.status + "）";
            try { var d = JSON.parse(txt); if (d && d.error) msg = d.error; } catch (e) { /* 非 JSON 用兜底文案 */ }
            throw new Error(msg);
          });
        }
        var name = "sign-" + date + ".log";
        var cd = resp.headers.get("Content-Disposition") || "";
        var m = /filename="?([^";]+)"?/.exec(cd);
        if (m && m[1]) name = m[1];
        return resp.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = url; a.download = name;
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
          YB.toast.success("已开始下载 " + name);
        });
      })
      .catch(function (e) { YB.toast.error((e && e.message) || "导出失败，请稍后再试"); })
      .then(function () {
        // 8 秒冷却：后端 60 秒内最多 6 次导出，连续点击只会触发 429
        setTimeout(function () { state.exporting = false; if (btn) btn.disabled = false; }, 8000);
      });
  }

  /* ---------------- 轮询 ---------------- */
  // 全部条件满足才刷新：页面可见、开关开、今天视图（历史静态）、无模态/下拉浮层、无在途请求
  function pollTick() {
    if (document.visibilityState !== "visible") return;
    if (!state.autoRefresh || state.viewDate) return;
    if (state.busy || state.exporting) return;
    if (document.querySelector(".pm-backdrop")) return;
    if (document.querySelector(".dd-wrap.is-open")) return;
    loadLogs(true);
  }

  /* ---------------- 事件绑定 ---------------- */
  function on(id, evt, fn) { var n = $(id); if (n) n.addEventListener(evt, fn); }

  function bind() {
    on("signin-select", "change", syncSigninBtn);
    on("signin-btn", "click", doSignin);
    on("export-btn", "click", exportLogs);

    var search = $("log-search"), timer = null;
    if (search) search.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        state.search = search.value.trim();
        resetSnap(); loadLogs();
      }, 300);
    });

    on("log-all-btn", "click", function () {
      state.all = !state.all;
      var b = $("log-all-btn");
      b.textContent = state.all ? "回到最近" : "显示全部";
      b.setAttribute("aria-pressed", String(state.all));
      resetSnap(); loadLogs();
    });
    on("log-view-btn", "click", function () {
      var v = String(($("log-date") || {}).value || "").trim();
      if (!v) { YB.toast.error("请先选择日期"); return; }
      if (!isValidDate(v)) { YB.toast.error("日期格式不正确，应为 YYYY-MM-DD"); return; }
      state.viewDate = v; writeUrlDate(v);
      resetSnap(); loadLogs();
    });
    on("log-today-btn", "click", function () {
      state.viewDate = ""; writeUrlDate("");
      var d = $("log-date"); if (d) d.value = "";
      resetSnap(); loadLogs();
    });
    on("log-auto-refresh", "change", function () {
      state.autoRefresh = $("log-auto-refresh").checked;
      try { localStorage.setItem(AUTO_KEY, state.autoRefresh ? "1" : "0"); } catch (e) { /* 隐私模式忽略 */ }
      if (state.autoRefresh) loadLogs();
    });

    Array.prototype.forEach.call(document.querySelectorAll(".log-collapse"), function (btn) {
      btn.addEventListener("click", function () {
        var open = btn.getAttribute("aria-expanded") !== "true";
        btn.setAttribute("aria-expanded", String(open));
        var body = document.getElementById(btn.getAttribute("aria-controls"));
        if (body) body.classList.toggle("is-open", open);
      });
    });
  }

  function init() {
    state.viewDate = readUrlDate();
    var dateInput = $("log-date");
    if (dateInput && state.viewDate) dateInput.value = state.viewDate;
    try {
      var saved = localStorage.getItem(AUTO_KEY);
      state.autoRefresh = saved === null ? true : saved === "1";
    } catch (e) { state.autoRefresh = true; }
    var auto = $("log-auto-refresh");
    if (auto) auto.checked = state.autoRefresh;

    bind();
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      loadAccounts();
      loadLogs();
      setInterval(pollTick, 10000);
      document.addEventListener("visibilitychange", function () { pollTick(); });
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
