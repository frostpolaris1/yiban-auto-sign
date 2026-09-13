/* 系统设置页（管理端 /settings）行为编排。

   依赖 core.js 与 components/{select-field,range-field,time-field,settings-schedule,
   settings-health,settings-notify,settings-mail,settings-quota,settings-switches}.js。

   职责：身份判定（is_builtin_admin）→ 分区（模板 .tabs，切换由 core.js 承担）与
   ?tab= 深链 → 拉取 GET /api/settings 回填各分区 → 公告读写 → 装配各组件。

   **保存语义（本页唯一口径）**：每个分区/卡片的字段改动只标脏，由各自的保存按钮提交；
   页面级只做两件事 —— 汇总脏分区、在"要离开这些改动"时问一句。带破坏性的按钮
   （清空收件人 / 恢复默认调度 / 暂停签到）不属于表单值，保持即时执行 + 二次确认。

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

  /* ---------------- 公告（任意管理员） ---------------- */
  var annBusy = false;
  var ann = { text: "", dirty: false };
  function annSetDirty(on) {
    ann.dirty = !!on;
    var btn = $("set-ann-save");
    if (btn) btn.hidden = !on;
    var badge = $("set-ann-dirty");
    if (badge) badge.hidden = !on;
  }
  function annTip(text, bad) {
    var tip = $("set-ann-tip");
    if (!tip) return;
    tip.textContent = text || "";
    tip.className = bad ? "set-tip set-bad" : "set-tip";
  }
  function loadAnnouncement() {
    return YB.api("GET", "/api/announcement").then(function (data) {
      ann.text = (data && data.text) || "";
      var input = $("set-announcement");
      if (input) input.value = ann.text;
      annSetDirty(false);
      annTip("", false);
    }).catch(function () { /* 公告读取失败不阻塞整页 */ });
  }
  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 失败。
  function saveAnnouncement(text) {
    if (annBusy) return Promise.resolve(false);
    if (text === ann.text) {
      annSetDirty(false);
      YB.toast.info("没有需要保存的改动");
      return Promise.resolve(true);
    }
    annBusy = true;
    annTip("保存中…", false);
    return YB.api("PUT", "/api/announcement", { text: text }).then(function (data) {
      ann.text = text;
      annSetDirty(false);
      annTip((data && data.msg) || (text ? "公告已更新" : "公告已清除"), false);
      return true;
    }, function (e) {
      annTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) { annBusy = false; return ok; });
  }
  function bindAnnouncement() {
    var input = $("set-announcement");
    if (input) {
      // 后端禁换行：前端也拦住粘贴/输入的换行（避免提交后才 400）
      input.addEventListener("input", function () {
        var v = input.value.replace(/[\r\n\u2028\u2029]+/g, " ");
        if (v !== input.value) input.value = v;
        annSetDirty(v !== ann.text);
      });
    }
    var save = $("set-ann-save");
    if (save) save.addEventListener("click", function () {
      saveAnnouncement((($("set-announcement") || {}).value || "").trim());
    });
    var clr = $("set-ann-clear");
    if (clr) clr.addEventListener("click", function () {
      YB.confirmDialog({
        title: "清除公告",
        body: "清除后所有页面顶部的公告都会消失。确定继续？",
        confirmText: "清除", danger: true
      }).then(function (ok) {
        if (!ok) return;
        var input2 = $("set-announcement");
        if (input2) input2.value = "";
        saveAnnouncement("");
      });
    });
  }

  /* ---------------- 未保存改动的统一守卫 ----------------
     各组件自报 isDirty()，页面只做汇总；切换分区、点站内链接离开时先问一句：
     保存并继续 / 放弃修改 / 取消。关标签页与刷新走组件的 beforeunload 兜底。 */
  function stores() {
    return [
      { name: "签到调度", get: function () { return YB.settingsSchedule; } },
      { name: "全局公告", get: function () { return { isDirty: function () { return ann.dirty; }, save: function () { return saveAnnouncement((($("set-announcement") || {}).value || "").trim()); } }; } },
      { name: "消息推送", get: function () { return YB.settingsNotify; } },
      { name: "邮件通知", get: function () { return YB.settingsMail; } },
      { name: "容量配额", get: function () { return YB.settingsQuota; } },
      { name: "健康与探针", get: function () { return YB.settingsHealth; } }
    ];
  }
  function dirtyStores() {
    return stores().filter(function (s) {
      var api = s.get();
      return !!(api && api.isDirty && api.isDirty());
    });
  }
  // 分区标签 → 该分区承载的脏源名（一个分区可含多个脏源：通知通道 = 推送 + 邮件）
  var DIRTY_TAB_STORES = {
    schedule: ["签到调度"],
    announcement: ["全局公告"],
    notify: ["消息推送", "邮件通知"],
    quota: ["容量配额"],
    health: ["健康与探针"]
  };
  // 脏徽标只在各自卡内 → 切到别的分区就看不见。按 dirtyStores() 汇总到分区标签的脏点上。
  function refreshTabDirty() {
    var names = dirtyStores().map(function (s) { return s.name; });
    tabLinks().forEach(function (t) {
      var owners = DIRTY_TAB_STORES[t.getAttribute("data-tab-target")];
      var dirty = !!owners && owners.some(function (n) { return names.indexOf(n) !== -1; });
      t.classList.toggle("is-dirty", dirty);
      if (dirty) t.title = "有未保存的修改";
      else t.removeAttribute("title");
    });
  }
  // 各组件在标脏/清脏时切换卡内徽标的 hidden；监听它即可在一次改动后同步标签脏点
  function observeDirtyBadges() {
    if (!window.MutationObserver) return;
    var badgeIds = ["ss-dirty", "set-ann-dirty", "sn-dirty", "sm-dirty", "set-cap-dirty", "sh-dirty"];
    var obs = new MutationObserver(refreshTabDirty);
    badgeIds.forEach(function (id) {
      var badge = $(id);
      if (badge) obs.observe(badge, { attributes: true, attributeFilter: ["hidden"] });
    });
  }
  function openUnsavedDialog(names, withKeep) {
    return new Promise(function (resolve) {
      var settled = false;
      function pick(v) { if (!settled) { settled = true; resolve(v); } }
      var body = YB.el("div", { class: "pm-confirm-text" });
      body.appendChild(YB.el("p", { text: "以下分区有尚未保存的修改：" + names.join("、") + "。" }));
      body.appendChild(YB.el("p", { text: "「保存并继续」先提交修改；「放弃修改」恢复原始值。" }));
      if (withKeep) {
        body.appendChild(YB.el("p", { text: "「保留修改继续查看」只切换分区，改动暂不提交。" }));
      }
      var actions = [
        { label: "取消", variant: "ghost", onClick: function () { pick("cancel"); } }
      ];
      // 第四分支仅用于分区切换：切过去但不清脏、不提交，便于对照另一个分区
      if (withKeep) {
        actions.push({ label: "保留修改继续查看", variant: "ghost", onClick: function () { pick("keep"); } });
      }
      actions.push({ label: "放弃修改", variant: "danger", onClick: function () { pick("discard"); } });
      actions.push({ label: "保存并继续", variant: "primary", onClick: function () { pick("save"); } });
      YB.openModal({
        title: "有未保存的修改",
        body: body,
        onClose: function () { pick("cancel"); },
        actions: actions
      });
    });
  }
  // 依次提交每个脏分区；任一取消/失败即中止（已提交的保持已提交，未提交的保留脏状态）
  function saveDirty(list) {
    return list.reduce(function (p, s) {
      return p.then(function (ok) {
        if (!ok) return false;
        var api = s.get();
        if (!api || !api.save) return true;
        return api.save();
      });
    }, Promise.resolve(true));
  }
  // 放弃修改 = 重新拉一遍服务端值覆盖本地（比重放每个组件的回滚逻辑更不容易漏）
  function reloadAll() {
    return Promise.all([
      YB.api("GET", "/api/settings").then(function (data) { applySettings(data); }),
      loadAnnouncement(),
      state.isMaster ? YB.settingsNotify.load() : Promise.resolve(),
      state.isMaster ? YB.settingsMail.load() : Promise.resolve()
    ]).catch(function () {}).then(function () { refreshTabDirty(); });
  }

  function guardThen(run, withKeep) {
    var list = dirtyStores();
    if (!list.length) { run(); return; }
    openUnsavedDialog(list.map(function (s) { return s.name; }), withKeep).then(function (choice) {
      if (choice === "cancel") return;
      if (choice === "keep") { run(); return; }   // 保留修改，仅切换视图
      if (choice === "discard") { reloadAll().then(run); return; }
      saveDirty(list).then(function (ok) { refreshTabDirty(); if (ok) run(); });
    });
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
        guardThen(function () { selectTab(t.getAttribute("data-tab-target"), true); }, true);
      });
    });
    var want = null;
    try { want = new URLSearchParams(location.search).get("tab"); } catch (e) { want = null; }
    if (want && validTab(want)) selectTab(want, false);
  }

  /* ---------------- 未保存改动的站内离场守卫 ---------------- */
  // 有未保存改动时，点侧边栏/面包屑等站内链接先弹**项目自己的**确认框（三分支）。
  // 不能只靠浏览器原生 beforeunload：部分内嵌浏览器不渲染该原生弹窗，表现为
  // 「点了链接没反应、也没有任何提示」。原生守卫仍在 settings-schedule.js 里
  // 作为关标签页/刷新的兜底；用户确认后由 markLeaving() 放行，避免二次拦截。
  function bindLeaveGuard() {
    document.addEventListener("click", function (e) {
      var t = e.target;
      var a = t && t.closest ? t.closest("a[href]") : null;
      if (!a) return;
      var raw = a.getAttribute("href") || "";
      if (!raw || raw.charAt(0) === "#") return;                    // 分区 tab / 页内锚点不拦
      if (a.target === "_blank" || a.hasAttribute("download")) return;
      if (/^(mailto:|tel:|javascript:)/i.test(raw)) return;
      if (!dirtyStores().length) return;
      e.preventDefault();
      e.stopPropagation();
      guardThen(function () {
        if (YB.settingsSchedule && YB.settingsSchedule.markLeaving) YB.settingsSchedule.markLeaving();
        location.href = a.href;                                     // a.href 已是绝对地址
      }, false);
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

      // 自研控件必须先建出可见体：各组件随后要按权限禁用它们（隐藏 input 上置 disabled 不可见）
      if (YB.selectField) YB.selectField.mount();
      if (YB.rangeField) YB.rangeField.mount();
      if (YB.timeField) YB.timeField.mount();
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
      // 脏点同步双保险：徽标 hidden 变化（MutationObserver）+ 输入/变更事件
      observeDirtyBadges();
      document.addEventListener("input", refreshTabDirty, true);
      document.addEventListener("change", refreshTabDirty, true);
      refreshTabDirty();
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
