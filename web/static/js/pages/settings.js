/* 系统设置页（管理端 /settings）行为编排。

   依赖 core.js 与 components/{settings-schedule,settings-health,settings-notify,
   settings-mail,settings-quota,settings-switches}.js。

   职责：身份判定（is_builtin_admin）→ 分区（模板 .tabs，切换由 core.js 承担）与
   ?tab= 深链 → 拉取 GET /api/settings 回填各分区 → 公告读写 → 装配六个组件。

   字段级权限（逐字段复刻后端内联判定；UI 禁用不是安全边界，高危请求仍带 confirm_password）：
     · 任意管理员：周六/周日、account_verify / probe_*、公告
     · 仅主管理员：调度（排序/分布/掐头去尾/间隔/窗口/自选）、容量上限、通知通道、
       系统开关（整 tab 隐藏）
   安全：全页零 innerHTML；不打印后端 e.data；写请求走 YB.api（自带 CSRF）。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;

  var state = { isMaster: false, capacityEst: null };

  /* ---------------- 页面级状态条（加载中 / 失败 + 重试） ---------------- */
  var statusTimer = null;
  function setStatus(tone, text, retry) {
    var box = $("set-status");
    if (!box) return;
    box.classList.remove("info", "danger");
    box.classList.add(tone);
    var t = $("set-status-text");
    if (t) t.textContent = text;
    var btn = box.querySelector("[data-set-retry]");
    if (btn) btn.hidden = !retry;
    if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
    // 短请求（本地通常 <80ms）不显示加载条：状态条在文档流内，显示即把 tabs 下推，
    // 「闪一下 + 顶一下」比不提示更差。失败态立即显示（用户需要看到错误与重试入口）。
    if (retry) { box.hidden = false; return; }
    statusTimer = setTimeout(function () { box.hidden = false; }, 200);
  }
  function hideStatus() {
    if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
    var box = $("set-status");
    if (box) box.hidden = true;
  }

  /* ---------------- 分区与深链 ?tab= ---------------- */
  function tabLinks() {
    return [].slice.call(document.querySelectorAll("[data-tab-group] .tab[data-tab-target]"));
  }
  function validTab(id) {
    return tabLinks().some(function (t) {
      return t.getAttribute("data-tab-target") === id && !t.hidden;
    });
  }
  function selectTab(id, writeUrl) {
    if (!validTab(id)) return;
    YB.switchTab(id);
    if (writeUrl === false) return;
    // 与 /logs?date= 同口径：replaceState 不产生历史堆积
    try {
      var u = new URL(location.href);
      u.searchParams.set("tab", id);
      history.replaceState(null, "", u.pathname + u.search + u.hash);
    } catch (e) { /* 受限环境忽略 */ }
  }
  function initTabs() {
    tabLinks().forEach(function (t) {
      t.addEventListener("click", function () {
        selectTab(t.getAttribute("data-tab-target"), true);
      });
    });
    var want = null;
    try { want = new URLSearchParams(location.search).get("tab"); } catch (e) { want = null; }
    if (want && validTab(want)) selectTab(want, false);
  }

  /* ---------------- 公告（任意管理员） ---------------- */
  var annBusy = false;
  function loadAnnouncement() {
    return YB.api("GET", "/api/announcement").then(function (data) {
      var input = $("set-announcement");
      if (input) input.value = (data && data.text) || "";
    }).catch(function () { /* 公告读取失败不阻塞整页 */ });
  }
  function saveAnnouncement(text) {
    if (annBusy) return;
    annBusy = true;
    var tip = $("set-ann-tip");
    if (tip) { tip.textContent = "保存中…"; tip.className = "set-tip"; }
    YB.api("PUT", "/api/announcement", { text: text }).then(function (data) {
      if (tip) tip.textContent = (data && data.msg) || (text ? "公告已更新" : "公告已清除");
    }).catch(function (e) {
      if (tip) { tip.textContent = (e && e.message) || "保存失败，请稍后重试"; tip.className = "set-tip set-bad"; }
    }).then(function () { annBusy = false; });
  }
  function bindAnnouncement() {
    var input = $("set-announcement");
    if (input) {
      // 后端禁换行：前端也拦住粘贴/输入的换行（避免提交后才 400）
      input.addEventListener("input", function () {
        var v = input.value.replace(/[\r\n\u2028\u2029]+/g, " ");
        if (v !== input.value) input.value = v;
      });
    }
    var save = $("set-ann-save");
    if (save) save.addEventListener("click", function () {
      saveAnnouncement((($("set-announcement") || {}).value || "").trim());
    });
    var clr = $("set-ann-clear");
    if (clr) clr.addEventListener("click", function () {
      var input2 = $("set-announcement");
      if (input2) input2.value = "";
      saveAnnouncement("");
    });
  }

  /* ---------------- 未保存改动的站内离场守卫 ---------------- */
  // 有未保存的调度改动时，点侧边栏/面包屑等站内链接先弹**项目自己的**确认框。
  // 不能只靠浏览器原生 beforeunload：部分内嵌浏览器不渲染该原生弹窗，表现为
  // 「点了链接没反应、也没有任何提示」。原生守卫仍在 settings-schedule.js 里
  // 作为关标签页/刷新的兜底；用户确认后由 markLeaving() 放行，避免二次拦截。
  function bindLeaveGuard() {
    document.addEventListener("click", function (e) {
      var sched = YB.settingsSchedule;
      if (!sched || !sched.isDirty || !sched.isDirty()) return;
      var t = e.target;
      var a = t && t.closest ? t.closest("a[href]") : null;
      if (!a) return;
      var raw = a.getAttribute("href") || "";
      if (!raw || raw.charAt(0) === "#") return;                    // 分区 tab / 页内锚点不拦
      if (a.target === "_blank" || a.hasAttribute("download")) return;
      if (/^(mailto:|tel:|javascript:)/i.test(raw)) return;
      e.preventDefault();
      e.stopPropagation();
      YB.confirmDialog({
        title: "有未保存的修改",
        body: "离开将丢失未保存的调度改动。确定离开吗？",
        confirmText: "离开", danger: true
      }).then(function (ok) {
        if (!ok) return;
        sched.markLeaving();
        location.href = a.href;                                     // a.href 已是绝对地址
      });
    }, true);
  }

  /* ---------------- 装配与加载 ---------------- */
  function applySettings(data) {
    state.capacityEst = data.capacity_estimate || null;
    YB.settingsSchedule.apply(data);
    YB.settingsHealth.apply(data);
    YB.settingsQuota.apply(data);
    YB.settingsSwitches.apply(data);
  }

  // 核心设置加载：加载中显状态条，失败显「设置加载失败」+ 重试；成功隐藏状态条。
  function loadAll() {
    setStatus("info", "正在加载设置…", false);
    return YB.api("GET", "/api/settings").then(function (data) {
      applySettings(data);
      hideStatus();
    }, function (e) {
      setStatus("danger", "设置加载失败", true);
      YB.toast.error((e && e.message) || "设置加载失败，请稍后重试");
      throw e;
    });
  }

  // 首屏与页面级重试共用：核心设置 + 两张主管理员专属卡。非主管理员不拉通知/邮件
  // （整 tab 已禁用，拉回来只会渲染出"看起来可编辑"的行；后端 GET 也会返回脱敏数据）。
  function startLoad() {
    return loadAll().then(function () {
      if (!state.isMaster) return;
      YB.settingsNotify.load();
      YB.settingsMail.load();
    });
  }

  // 保存容量上限后只刷新设置（容量配额 + 调度警示），不重放两张通知卡以免抹掉未保存编辑。
  function refreshAfterQuotaSave() {
    return YB.api("GET", "/api/settings").then(function (data) {
      state.capacityEst = data.capacity_estimate || null;
      YB.settingsQuota.apply(data);
      YB.settingsSchedule.refreshWarn();
    }).catch(function () {});
  }

  function init() {
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      state.isMaster = !!me.is_builtin_admin;
      // 系统开关整 tab 仅主管理员可见（权限判定在后端，隐藏只是界面口径）
      var tab = $("set-tab-switches");
      if (tab) tab.hidden = !state.isMaster;
      var panel = document.querySelector('[data-tab-group] .tab-panel[data-tab-id="switches"]');
      if (panel) panel.hidden = !state.isMaster;

      if (YB.timeField) YB.timeField.mount();   // 时/分 select 初始化（须在组件读写之前）
      YB.settingsSchedule.mount({
        isMaster: state.isMaster,
        capacity: function () { return state.capacityEst; }
      });
      YB.settingsHealth.mount();
      YB.settingsNotify.mount({ isMaster: state.isMaster });
      YB.settingsMail.mount({ isMaster: state.isMaster });
      YB.settingsQuota.mount({ isMaster: state.isMaster, onSaved: refreshAfterQuotaSave });
      YB.settingsSwitches.mount({ isMaster: state.isMaster });
      bindAnnouncement();
      initTabs();
      bindLeaveGuard();
      // 状态条重试走事件委托（按钮是模板静态节点，无需逐次绑定）
      document.addEventListener("click", function (e) {
        var t = e.target;
        if (t && t.closest && t.closest("[data-set-retry]")) startLoad().catch(function () {});
      });
      loadAnnouncement();
      startLoad().catch(function () {});
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
