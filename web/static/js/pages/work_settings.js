/* 系统设置页（管理端 /settings）行为编排。

   依赖 core.js 与 components/{select-field,range-field,time-field,settings-schedule,
   settings-health,settings-notify,settings-mail,settings-quota,settings-executors,
   settings-switches}.js。

   职责：身份判定（is_builtin_admin）→ 分区（模板 .tabs，切换由 core.js 承担）与
   ?tab= 深链（core.js 共享助手 tabDeepLink/selectTab，写 URL 时机由本页脏守卫控制）→
   拉取 GET /api/settings 回填各分区 → 公告读写 → 装配各组件。

   **保存语义（本页唯一口径）**：每个分区/卡片的字段改动只标脏，由各自的保存按钮提交；
   页面级只做两件事 —— 汇总脏分区、在"要离开这些改动"时问一句。带破坏性的按钮
   （清空收件人 / 恢复默认调度 / 暂停签到）不属于表单值，保持即时执行 + 二次确认。

   字段级权限（逐字段复刻后端内联判定；UI 禁用不是安全边界，高危请求仍带 confirm_password）：
     · 任意管理员：周六/周日、account_verify / probe_*、公告
     · 仅主管理员：调度（排序/分布/掐头去尾/间隔/窗口/自选）、容量上限、通知通道、
       执行体与出口、系统开关（后两者整 tab 隐藏）
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

  /* ---------------- 公告（双人发布：任意管理员写草稿，主管理员发布/下线） ----------------
     GET /api/announcement 对管理员返回 {text(线上), draft, draft_by, draft_at,
     published_by, published_at}；PUT 只写草稿（线上不变）；POST /api/announcement/publish
     仅主管理员 + 当次口令（body.confirm_password）：草稿非空 → 发布并清空草稿；
     草稿为空且线上有内容 → 下线；两者皆空 → 400。 */
  var annBusy = false;
  var ann = { draft: "", draftBy: "", draftAt: "", text: "", publishedBy: "", publishedAt: "", dirty: false };
  function annInput() { return $("set-announcement"); }
  function annCurrentText() {
    var n = annInput();
    return n ? String(n.value || "").trim() : "";
  }
  function annTip(text, bad) { YB.setTip("set-ann-tip", text, bad); }
  function annMeta(by, at) {
    var parts = [];
    if (by) parts.push("由 " + by);
    if (at) parts.push(at);
    return parts.join(" · ");
  }
  function annSetDirty(on) {
    ann.dirty = !!on;
    var btn = $("set-ann-save");
    if (btn) btn.hidden = !on;
    var badge = $("set-ann-dirty");
    if (badge) badge.hidden = !on;
    renderAnnPublish();
  }
  function renderAnnDraftMeta() {
    var n = $("set-ann-draft-meta");
    if (!n) return;
    var meta = annMeta(ann.draftBy, ann.draftAt);
    n.textContent = ann.draft ? ("草稿" + (meta ? "：" + meta : "已保存")) : "（无草稿）";
  }
  function renderAnnPublished() {
    var text = $("set-ann-published"), meta = $("set-ann-published-meta");
    if (text) text.textContent = ann.text || "（当前没有线上公告）";
    if (meta) meta.textContent = ann.text ? annMeta(ann.publishedBy, ann.publishedAt) : "";
  }
  // 发布按钮三态（按**当前草稿**判，含未保存的编辑）：非空→「发布公告」；
  // 空且线上有内容→「下线线上公告」；两者皆空→禁用（与后端 400 同口径，前端先挡住误按）。
  // 非主管理员：按钮保留但禁用并通过 #set-ann-perm 就地说明原因，不藏起来让人不知为何点不动。
  function renderAnnPublish() {
    var btn = $("set-ann-publish");
    if (!btn) return;
    var offline = !annCurrentText() && !!ann.text;
    var noop = !annCurrentText() && !ann.text;
    btn.hidden = false;                                  // 按钮常驻，权限/空态用 disabled 表达
    btn.textContent = offline ? "下线线上公告" : "发布公告";
    btn.disabled = !state.isMaster || noop;
    if (!state.isMaster) {
      btn.title = "发布/下线线上公告仅主管理员可做";
      btn.setAttribute("aria-describedby", "set-ann-perm");
    } else if (noop) {
      btn.title = "草稿与线上公告都为空，没有可执行的操作";
      btn.removeAttribute("aria-describedby");
    } else {
      btn.removeAttribute("title");
      btn.removeAttribute("aria-describedby");
    }
    var perm = $("set-ann-perm");
    if (perm) perm.hidden = state.isMaster;
  }
  function loadAnnouncement() {
    return YB.api("GET", "/api/announcement").then(function (data) {
      data = data || {};
      ann.draft = String(data.draft || "");
      ann.draftBy = String(data.draft_by || "");
      ann.draftAt = String(data.draft_at || "");
      ann.text = String(data.text || "");
      ann.publishedBy = String(data.published_by || "");
      ann.publishedAt = String(data.published_at || "");
      var input = annInput();
      if (input) input.value = ann.draft;
      renderAnnDraftMeta();
      renderAnnPublished();
      annSetDirty(false);
      annTip("", false);
    }).catch(function () { /* 公告读取失败不阻塞整页 */ });
  }
  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 失败。
  // 只写草稿：保留后端 msg（"草稿已保存，待主管理员发布" / "草稿已清除（线上公告未变…）"）。
  function saveAnnouncement(text) {
    if (annBusy) return Promise.resolve(false);
    if (text === ann.draft) {
      annSetDirty(false);
      YB.toast.info("没有需要保存的改动");
      return Promise.resolve(true);
    }
    annBusy = true;
    annTip("保存中…", false);
    return YB.api("PUT", "/api/announcement", { text: text }).then(function (data) {
      ann.draft = text;
      // 重拉一次拿后端回写的草稿作者/时刻（不在前端猜作者）
      return loadAnnouncement().then(function () {
        annTip((data && data.msg) || (text ? "草稿已保存" : "草稿已清除"), false);
        return true;
      });
    }, function (e) {
      annTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) { annBusy = false; return ok; },
            function (e) { annBusy = false; throw e; });
  }
  // 发布/下线：先确认影响面 → 口令框收当次口令；回调**返回请求 Promise**（既有契约），
  // 失败会在框内显示并可改口令重试。发布作用于已保存的草稿，未保存的编辑先自动落草稿，
  // 避免"看起来发了新内容、其实发的是旧草稿"。
  function publishAnnouncement() {
    if (annBusy || !state.isMaster) return;
    if (!annCurrentText() && !ann.text) return;          // 按钮已禁用，这里防 DOM 篡改
    var offline = !annCurrentText() && !!ann.text;
    YB.confirmDialog({
      title: offline ? "下线线上公告" : "发布公告",
      body: offline
        ? "下线后所有页面顶部（含登录页）的公告都会消失。确认继续？"
        : "发布后公告会立即出现在所有页面顶部（含登录页）。确认继续？",
      confirmText: offline ? "下线" : "发布",
      danger: offline
    }).then(function (ok) {
      if (!ok) return;
      YB.openConfirmPasswordModal(
        (offline ? "下线全站公告" : "发布全站公告") + "会影响所有访问者。请输入当前管理员密码确认。",
        function (pw) { return submitPublish(pw); });
    });
  }
  function submitPublish(pw) {
    annBusy = true;
    annTip("发布中…", false);
    var chain = Promise.resolve();
    if (ann.dirty) {
      chain = YB.api("PUT", "/api/announcement", { text: annCurrentText() }).then(function () {
        ann.draft = annCurrentText();
        annSetDirty(false);
      });
    }
    return chain.then(function () {
      return YB.api("POST", "/api/announcement/publish", { confirm_password: pw });
    }).then(function (data) {
      YB.toast.success((data && data.msg) || "已发布");
      // 线上文本已变：先就地刷新外壳横幅（不再等一次 GET），再重拉本卡拿到发布人/时刻
      YB.applyAnnouncementText(data && data.text);
      return loadAnnouncement().then(function () { annTip((data && data.msg) || "已发布", false); });
    }).then(function () { annBusy = false; },
            // 原样上抛：口令框显示错误并保持打开供改口令重试；失败不动已保存的草稿
            function (e) { annBusy = false; throw e; });
  }
  function bindAnnouncement() {
    var input = annInput();
    if (input) {
      // 后端禁换行：前端也拦住粘贴/输入的换行（避免提交后才 400）
      input.addEventListener("input", function () {
        var v = input.value.replace(/[\r\n\u2028\u2029]+/g, " ");
        if (v !== input.value) input.value = v;
        annSetDirty(v.trim() !== ann.draft);
      });
    }
    var save = $("set-ann-save");
    if (save) save.addEventListener("click", function () { saveAnnouncement(annCurrentText()); });
    var pub = $("set-ann-publish");
    if (pub) pub.addEventListener("click", publishAnnouncement);
    var clr = $("set-ann-clear");
    if (clr) clr.addEventListener("click", function () {
      YB.confirmDialog({
        title: "清除草稿",
        body: "清除后草稿会被删除，线上公告不受影响；如需撤下线上公告请点「下线线上公告」。确定继续？",
        confirmText: "清除", danger: true
      }).then(function (ok) {
        if (!ok) return;
        var input2 = annInput();
        if (input2) input2.value = "";
        saveAnnouncement("");
      });
    });
    renderAnnPublish();
  }

  /* ---------------- 未保存改动的统一守卫 ----------------
     各组件自报 isDirty()，页面只做汇总；切换分区、点站内链接离开时先问一句：
     保存并继续 / 放弃修改 / 取消。关标签页与刷新走组件的 beforeunload 兜底。 */
  function stores() {
    return [
      { name: "签到调度", get: function () { return YB.settingsSchedule; } },
      { name: "全局公告", get: function () { return { isDirty: function () { return ann.dirty; }, save: function () { return saveAnnouncement(annCurrentText()); } }; } },
      { name: "消息推送", get: function () { return YB.settingsNotify; } },
      { name: "邮件通知", get: function () { return YB.settingsMail; } },
      { name: "容量配额", get: function () { return YB.settingsQuota; } },
      { name: "执行体", get: function () { return YB.settingsExecutors; } },
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
    executors: ["执行体"],
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
    // 执行体分区已改清单式（写操作即时落盘），没有卡内脏徽标，故不在此列
    var badgeIds = ["ss-dirty", "set-ann-dirty", "sn-dirty", "sm-dirty", "set-cap-dirty",
                    "sh-dirty"];
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
      state.isMaster ? YB.settingsMail.load() : Promise.resolve(),
      state.isMaster ? YB.settingsExecutors.load() : Promise.resolve()
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
  // 深链与 URL 同步已收进 core.js 的共享助手（tabDeepLink / selectTab / tabSyncUrl，
  // 全站唯一一份）；本页组在模板上标 data-tab-url-own（自管），因为切换要过脏守卫：
  // 写 URL 的时机必须等守卫放行，由下面的 selectTab(…, { syncUrl: true }) 显式触发，
  // 委托路径不写 —— 守卫取消时地址栏不残留 ?tab=。
  function tabLinks() {
    return [].slice.call(document.querySelectorAll("[data-tab-group] .tab[data-tab-target]"));
  }
  function initTabs() {
    tabLinks().forEach(function (t) {
      t.addEventListener("click", function () {
        guardThen(function () { YB.selectTab(t.getAttribute("data-tab-target"), { syncUrl: true }); }, true);
      });
    });
    YB.tabDeepLink();
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
    state.capacity = data.capacity || null;
    YB.settingsSchedule.apply(data);
    YB.settingsHealth.apply(data);
    YB.settingsQuota.apply(data);
    YB.settingsSwitches.apply(data);
    // 执行体分区的规模 KPI 要用「容量配额 · 用户容量上限」（另一份数据源）：
    // 上限改了（保存容量上限后重拉设置也走这里）一并重画，避免同页两处数字打架。
    if (YB.settingsExecutors && YB.settingsExecutors.refreshKpis) YB.settingsExecutors.refreshKpis();
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

  // 首屏与页面级重试共用：核心设置 + 两张主管理员专属卡 + 执行体分区。非主管理员不拉
  // 通知/邮件/执行体——理由是整 tab 已隐藏，拉回来只会渲染出"看起来可编辑"的行。
  // 别把这里当成"后端会 403"：两个配置 GET 对任意管理员返回 200（通道状态字段本就有意
  // 可读），执行体 GET 才硬拒主管理员以外。口径见 settings-notify.js 头注。
  function startLoad() {
    return loadAll().then(function () {
      if (!state.isMaster) return;
      YB.settingsNotify.load();
      YB.settingsMail.load();
      YB.settingsExecutors.load();
    });
  }

  // 保存容量上限后只刷新设置（容量配额 + 调度警示），不重放两张通知卡以免抹掉未保存编辑。
  // 容量上限还是执行体分区那张「平均每执行体分到的人数」的分子，故一并重画该卡。
  function refreshAfterQuotaSave() {
    return YB.api("GET", "/api/settings").then(function (data) {
      state.capacityEst = data.capacity_estimate || null;
      state.capacity = data.capacity || null;
      YB.settingsQuota.apply(data);
      YB.settingsSchedule.refreshWarn();
      if (YB.settingsExecutors && YB.settingsExecutors.refreshKpis) YB.settingsExecutors.refreshKpis();
    }).catch(function () {});
  }

  function init() {
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      state.isMaster = !!me.is_builtin_admin;
      // 系统开关、执行体两个整 tab 仅主管理员可见（权限判定在后端，隐藏只是界面口径）：
      // 非主管理员连请求都不发（见 startLoad），后端 403 仍是兜底。
      [["set-tab-switches", "switches"], ["set-tab-executors", "executors"]].forEach(function (pair) {
        var tab = $(pair[0]);
        if (tab) tab.hidden = !state.isMaster;
        var panel = document.querySelector('[data-tab-group] .tab-panel[data-tab-id="' + pair[1] + '"]');
        if (panel) panel.hidden = !state.isMaster;
      });

      // 自研控件必须先建出可见体：各组件随后要按权限禁用它们（隐藏 input 上置 disabled 不可见）
      if (YB.selectField) YB.selectField.mount();
      if (YB.rangeField) YB.rangeField.mount();
      if (YB.timeField) YB.timeField.mount();
      YB.settingsSchedule.mount({
        isMaster: state.isMaster,
        capacity: function () { return state.capacityEst; }
      });
      YB.settingsHealth.mount({ isMaster: state.isMaster });
      YB.settingsNotify.mount({ isMaster: state.isMaster });
      YB.settingsMail.mount({ isMaster: state.isMaster });
      YB.settingsQuota.mount({ isMaster: state.isMaster, onSaved: refreshAfterQuotaSave });
      // 执行体分区：容量上限由页面注入（规模 KPI 的分子）；同一份响应里的容量建议
      // （measured / recommendation / window / current_accounts）转交「容量配额」分区的建议卡——
      // 一份数据一次请求，两个分区各取所需（用户 2026-09-17：这两件事共用同一份数据）。
      YB.settingsExecutors.mount({
        isMaster: state.isMaster,
        capacity: function () { return state.capacity; },
        onData: function (data) { YB.settingsQuota.applyExecutors(data); }
      });
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
