/* 系统设置 · 执行体与出口分区（管理端 /settings 的「执行体」tab）。

   挂载到 window.YB.settingsExecutors；classic script。

   数据源与同页其它分区**不同**：本分区读写 GET/PUT /api/scheduler/executors
   （仅主管理员），不来自 /api/settings，故自带 load()（形态与 settings-notify 的
   load() 一致，由 pages/work_settings.js 在身份判定后调用）。非主管理员：整 tab
   隐藏（页面编排负责），控件仍按权限禁用 + aria-describedby 就地说明 —— 与容量配额
   分区同一写法；禁用不是安全边界，后端 403 兜底。

   敏感信息（出口串可能含 user:pass@）：读接口只回**描述串**（scheme://host[:port]），
   所以编辑框一律留空并提示「留空 = 不修改」，保存成功后立即清空输入框；完整串既不入
   DOM 文本与属性，也不进 title、data-* 或控制台。配置项键名全部由接口下发（前端不写死
   任何一个），接口不给就整段提示留空。

   保存语义（与全页统一）：改动只标脏（脏徽标 + 保存按钮出现），点「保存执行体配置」
   才 PUT，且只提交**改动过的字段**（接口支持部分提交）。接口不涉及 confirm_password，
   故不弹口令框。成功后展示接口回的 note（含"下一轮定时任务或容器重启后生效"语义），
   该语义在卡片底部也提前写明，避免"点完就在跑"的误解。

   对外面：mount(options) / load() / apply(data) / save() → Promise<boolean> / isDirty()。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ctx = { isMaster: false };
  var snap = { workers: 1 };
  // 容量配额口径（来自 /api/settings，由页面在装配时喂进来）：账号上限 与 单执行体容量
  var quota = { maxAccounts: null, perExec: null };
  var lastData = null;          // 最近一次接口响应（行内详情弹窗按需读取，不重复请求）
  var dirty = false;
  var busy = false;

  // 可编辑控件（权限禁用与脏判定共用一份清单，避免两处漂移）
  var FIELDS = ["set-exec-workers", "set-exec-list", "set-exec-fb-proxy"];

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  function setText(id, text) {
    var n = $(id);
    if (n) n.textContent = text == null ? "" : String(text);
  }
  function setTip(text, bad) { YB.setTip("set-exec-tip", text, bad); }
  // 输入框读数：空/非数字回退到快照值（= 视为"未改动"，与容量配额分区的 value() 同口径）
  function num(id, fallback) {
    var n = parseInt(($(id) || {}).value, 10);
    return isNaN(n) ? fallback : n;
  }
  function text(id) { return (($(id) || {}).value || "").trim(); }
  function count(v) { return Number(v) || 0; }

  function markDirty() {
    if (dirty) return;
    dirty = true;
    setHidden($("set-exec-save"), false);
    setHidden($("set-exec-dirty"), false);
  }
  function clearDirty() {
    dirty = false;
    setHidden($("set-exec-save"), true);
    setHidden($("set-exec-dirty"), true);
  }

  // 权限：禁用控件时把原因 #set-exec-perm 与控件做程序化关联（读屏可及）。
  function applyPerm() {
    var disabled = !ctx.isMaster;
    FIELDS.forEach(function (id) {
      var n = $(id);
      if (!n) return;
      n.disabled = !!disabled;
      if (disabled) n.setAttribute("aria-describedby", "set-exec-perm");
      else n.removeAttribute("aria-describedby");
    });
    var btn = $("set-exec-save");
    if (btn) btn.disabled = !!disabled;
    var clr = $("set-exec-clear");
    if (clr) clr.disabled = !!disabled;
    setHidden(btn, disabled || !dirty);
    setHidden($("set-exec-dirty"), disabled || !dirty);
    setHidden($("set-exec-perm"), !disabled);
  }

  /* ---------------- 只读视图 ---------------- */
  // 一览表：一行一个执行体（含兜底），出口只读展示 + 建议账号数 + 行内「详情」。
  // 出口描述/键名/运行状态一律取自接口，本函数只做排版与文案。
  function paintWorkers(data) {
    var w = (data && data.workers) || {};
    var keys = w.env_keys || {};
    // 每个执行体的建议负载 ＝ 单执行体容量（与容量配额页同一口径，见 paintAdvice）
    var perExec = (quota.perExec == null ? null : count(quota.perExec));
    setText("set-exec-configured", "当前配置 " + count(w.configured || 1) + " 个并行执行体。");
    // 「列表未配 → 退回单执行体出口」的口径在 yiban/egress.py，键名由接口给出
    var hint = keys.list ? "对应配置项：" + keys.list
      + (keys.single ? "；未配置时退回 " + keys.single : "") : "";
    setText("set-exec-key-list", hint);

    var tbody = $("set-exec-assign");
    if (!tbody) return;
    tbody.textContent = "";                       // 清空容器（不用 innerHTML）
    var rows = (w.assignments || []);
    if (!rows.length) {
      tbody.appendChild(YB.el("tr", {}, [
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "暂无执行体分配" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { class: "set-exec-col-md" })
      ]));
    } else {
      rows.forEach(function (a) {
        var idx = count(a.index);
        tbody.appendChild(YB.el("tr", {}, [
          // 标识与配置下标一致（后端按 YIBAN_PROXY_LIST 下标分配出口）
          YB.el("td", { class: "mono", text: "worker-" + idx }),
          YB.el("td", { text: "并行" }),
          // 接口已脱敏（去 userinfo），逐字照显，前端不再二次加工描述串
          YB.el("td", { text: a.egress || "直连（本机出口）" }),
          // 「说明」列逐行自报语义（执行体＝建议账号数、兜底＝扫描间隔），
          // 避免同一列两种含义而列名只写了其中一种
          YB.el("td", { class: "set-exec-col-md", text: perExec == null ? "—" : "建议 " + perExec + " 个账号" }),
          YB.el("td", {}, [detailBtn("worker-" + idx, idx)])
        ]));
      });
    }
    // 兜底执行体也是表里的一行：存活状态只有它可判（按心跳新鲜度），并行执行体的存活未暴露
    var fb = (data && data.fallback) || {};
    tbody.appendChild(YB.el("tr", {}, [
      YB.el("td", {}, [
        YB.el("span", { text: "兜底执行体 " }),
        YB.el("span", { class: "badge dot " + (fb.alive === true ? "badge--ok" : "badge--bad"),
                        text: fb.alive === true ? "在跑" : "已停" })
      ]),
      YB.el("td", { text: "兜底" }),
      YB.el("td", { text: fb.egress || "直连（本机出口）" }),
      YB.el("td", { class: "set-exec-col-md", text: "扫描间隔 " + count(fb.interval_sec) + " 秒" }),
      YB.el("td", {}, [detailBtn("兜底执行体", null)])
    ]));
  }

  function detailBtn(label, idx) {
    var btn = YB.el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "详情" });
    btn.setAttribute("aria-label", label + " 详情");
    btn.addEventListener("click", function () { openDetail(idx); });
    return btn;
  }

  // 行内详情弹窗：复用站点既有弹窗外壳与只读字段排版（与账号/用户编辑弹窗同形）。
  // 只展示当前接口能给的：出口（已脱敏）、建议账号数 / 兜底扫描间隔与存活、对应配置项。
  // **出口就地编辑暂缓**：按序号写单个出口需要后端支持（见 docs/refactor/71 §3.5），
  // 未就绪前这里不放可编辑控件——避免"填了写不进去"或误清其它执行体的凭据。
  function openDetail(idx) {
    if (!lastData) { setTip("数据尚未加载完成", true); return; }
    var w = lastData.workers || {}, fb = lastData.fallback || {};
    var rec = lastData.recommendation || null;
    var isFb = idx == null;
    var rows, egressLabel;
    if (isFb) {
      egressLabel = "兜底出口（已脱敏）";
      rows = [
        ["运行状态", fb.alive === true ? "在跑" : "已停（窗口内不会自动补签）"],
        ["扫描间隔", count(fb.interval_sec) + " 秒"],
        ["对应配置项", fb.env_key || "—"]
      ];
    } else {
      egressLabel = "出口（已脱敏）";
      rows = [
        ["建议账号数", rec ? count(rec.per_executor_accounts) + " 个（实测容量 × 2/3，是建议不是上限）" : "未实测"],
        ["对应配置项", (w.env_keys && w.env_keys.list) || "—"]
      ];
    }
    var a = isFb ? {} : ((w.assignments || []).filter(function (x) {
      return count(x.index) === idx;
    })[0] || {});
    // form-grid 是两列：字段成对排；说明句放在网格之外，才能整行铺满（否则落进右格，
    // 看起来像挂在"对应配置项"旁边）
    var wrap = YB.el("div");
    var grid = YB.el("div", { class: "form-grid" });
    grid.appendChild(YB.el("div", { class: "field" }, [
      YB.el("span", { class: "field-label", text: egressLabel }),
      YB.el("p", { class: "field-help", text: (isFb ? fb.egress : a.egress) || "直连（本机出口）" })
    ]));
    rows.forEach(function (pair) {
      grid.appendChild(YB.el("div", { class: "field" }, [
        YB.el("span", { class: "field-label", text: pair[0] }),
        YB.el("p", { class: "field-help", text: pair[1] })
      ]));
    });
    wrap.appendChild(grid);
    wrap.appendChild(YB.el("p", {
      class: "field-help",
      text: "单个执行体的出口编辑需要后端支持按序号写入，接口就绪后开放。"
    }));
    YB.openModal({
      title: isFb ? "兜底执行体" : "worker-" + idx,
      body: wrap,
      actions: [{ label: "关闭", variant: "primary", onClick: function () { return true; } }]
    });
  }

  function paintFallback(data) {
    var fb = (data && data.fallback) || {};
    var badge = $("set-exec-fb-badge");
    var alive = fb.alive === true;
    if (badge) {
      badge.textContent = alive ? "在跑" : "已停";
      badge.className = "badge dot " + (alive ? "badge--ok" : "badge--bad");
    }
    // 一览表里兜底那行已给出出口与扫描间隔，此处只补一句状态说明（避免同一信息两处重复）
    setText("set-exec-fb-text", alive
      ? "兜底执行体正在运行，窗口内的漏签会由它补签。"
      : "兜底执行体当前未在运行。");
    setHidden($("set-exec-fb-warn"), alive);
    setText("set-exec-key-fb", fb.env_key ? "对应配置项：" + fb.env_key : "");
  }

  // 建议区（只读）：口径与「容量配额」分区**同源** —— 账号容量上限 ÷ 单执行体容量，向上取整。
  // 两个输入值都来自 /api/settings：quota.maxAccounts = capacity.accounts_max、
  // quota.perExec = capacity_estimate.accounts_cap（单执行体容量，已扣掐头去尾）。
  // 临时口径说明：按用户 2026-09-16 的定稿，这个建议值**应当由后端按同一公式算好下发**
  // （见 docs/refactor/73 §6）；在接口就绪前，这里用页面已有的两个数就地换算，只为让两个页面
  // 的数字当场对得上，不引入第二套算法（分子分母都来自后端）。
  function paintAdvice(data) {
    var win = (data && data.window) || {};
    var maxA = quota.maxAccounts, perExec = quota.perExec;
    setText("set-exec-quota", maxA == null ? ""
      : (maxA > 0 ? "账号容量上限：" + count(maxA) + " 个（系统设置 → 容量配额）"
                  : "账号容量上限为 0（不限），无法据此给出建议。"));
    setText("set-exec-window", perExec == null ? ""
      : "单执行体容量：" + count(perExec) + " 个（按有效签到窗口 "
        + (win.start && win.end ? win.start + " ~ " + win.end : "—")
        + " 与账号间隔估算，与容量配额页同一口径）。");
    setText("set-exec-accounts", "当前计入容量的账号数：" + count(data && data.current_accounts) + " 个");

    if (maxA > 0 && perExec > 0) {
      var need = Math.ceil(count(maxA) / count(perExec));
      setText("set-exec-advice-nums",
        "建议执行体数：" + need + " 个（= 账号容量上限 ÷ 单执行体容量，向上取整）。");
      setHidden($("set-exec-advice-nums"), false);
    } else {
      setText("set-exec-advice-nums", "");
      setHidden($("set-exec-advice-nums"), true);
    }
    // 建议不是上限：这句必须在场（契约要求，防止管理员读成"超过就出错"）
    var canAdvise = maxA > 0 && perExec > 0;
    setText("set-exec-advice-note", canAdvise
      ? "建议值按容量配额换算，只是提醒、不是程序上限；实际承载还受出口带宽与网络影响。"
      : "");
    setHidden($("set-exec-advice-note"), !canAdvise);
  }

  /* ---------------- 脏状态与提交 ---------------- */
  // 有改动 = 任一数值字段偏离快照，或任一出口输入框非空（出口读不回原值，非空即视为要写）
  function changed() {
    return num("set-exec-workers", snap.workers) !== snap.workers
      || !!text("set-exec-list") || !!text("set-exec-fb-proxy");
  }
  function syncDirty() {
    if (changed()) markDirty();
    else clearDirty();
  }
  // 部分提交：只带上改动过的字段，未提交的字段由后端保持原值
  function collect() {
    var body = {};
    var w = num("set-exec-workers", snap.workers);
    if (w !== snap.workers) body.workers = w;
    var list = text("set-exec-list");
    if (list) body.proxy_list = list;
    var fb = text("set-exec-fb-proxy");
    if (fb) body.proxy_fallback = fb;
    return body;
  }
  function clearProxyInputs() {
    ["set-exec-list", "set-exec-fb-proxy"].forEach(function (id) {
      var n = $(id);
      if (n) n.value = "";                      // 完整串不驻留 DOM
    });
  }
  // 提交后重读：只读区（分配表/运行状态/容量）要跟着新配置走；读失败不推翻"已提交"的结论
  function refreshAfterSave(body) {
    return YB.api("GET", "/api/scheduler/executors").then(apply, function () {
      if (body.workers != null) snap.workers = body.workers;
      clearProxyInputs();
      clearDirty();
      applyPerm();
    });
  }

  function submit(body) {
    busy = true;
    setTip("保存中…", false);
    return YB.api("PUT", "/api/scheduler/executors", body).then(function (data) {
      var note = (data && data.note) || "已保存";
      clearProxyInputs();
      // note 在重读之后写入（apply 会清空 .set-tip），否则会被回填流程抹掉
      return refreshAfterSave(body).then(function () {
        setTip(note, false);
        return true;
      });
    }, function (e) {
      // 400 的 error 原文即为校验提示（YB.api 把它放进 e.message），不吞成通用网络错误
      setTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      busy = false;
      applyPerm();
      return ok;
    });
  }

  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 失败或无权限
  function save() {
    if (busy || !ctx.isMaster) return Promise.resolve(false);
    var body = collect();
    if (!Object.keys(body).length) {
      clearDirty();
      setTip("没有需要保存的改动", false);
      return Promise.resolve(true);
    }
    return submit(body);
  }

  // 清空出口 = 把列表与兜底出口都写成空串（各执行体改走本机直连）。留空输入框的语义是
  // "不修改"，无法表达"清空"，故这一类动作单独给它一个入口（与邮件卡的「清空收件人」同形）。
  function clearEgress() {
    if (busy || !ctx.isMaster) return Promise.resolve(false);
    return YB.confirmDialog({
      title: "清空出口配置",
      body: "将把出口列表与兜底出口一并清空，各执行体与兜底执行体改走本机直连；"
        + "保存只写配置，下一轮定时任务或容器重启后生效。确定继续？",
      confirmText: "清空出口", danger: true
    }).then(function (ok) {
      if (!ok) return false;
      var body = { proxy_list: "", proxy_fallback: "" };
      busy = true;
      setTip("提交中…", false);
      return YB.api("PUT", "/api/scheduler/executors", body).then(function (data) {
        return refreshAfterSave(body).then(function () {
          setTip((data && data.note) || "出口已清空", false);
          return true;
        });
      }, function (e) {
        setTip((e && e.message) || "清空失败，请稍后重试", true);
        return false;
      }).then(function (ok2) {
        busy = false;
        applyPerm();
        return ok2;
      });
    });
  }

  function apply(data) {
    lastData = data || null;
    var w = (data && data.workers) || {};
    snap.workers = count(w.configured || 1);
    if ($("set-exec-workers")) $("set-exec-workers").value = String(snap.workers);
    clearProxyInputs();
    paintWorkers(data);
    paintFallback(data);
    paintAdvice(data);
    clearDirty();
    setTip("", false);
    applyPerm();
  }

  function load() {
    if (!ctx.isMaster) return Promise.resolve(false);
    return YB.api("GET", "/api/scheduler/executors").then(function (data) {
      apply(data);
      return true;
    }, function (e) {
      setTip((e && e.message) || "执行体配置读取失败，请稍后重试", true);
      return false;
    });
  }

  function mount(options) {
    ctx = { isMaster: !!(options && options.isMaster) };
    var btn = $("set-exec-save");
    if (btn) btn.addEventListener("click", function () { save(); });
    var clr = $("set-exec-clear");
    if (clr) clr.addEventListener("click", function () { clearEgress(); });
    FIELDS.forEach(function (id) {
      var n = $(id);
      if (!n) return;
      n.addEventListener("input", syncDirty);
      n.addEventListener("change", syncDirty);
    });
    applyPerm();
  }

  // 容量配额数据（/api/settings）：页面装配时喂进来，供建议区换算（见 paintAdvice 的临时口径说明）
  function applySettings(data) {
    var cap = (data && data.capacity) || {};
    var est = (data && data.capacity_estimate) || {};
    quota.maxAccounts = cap.accounts_max == null ? null : count(cap.accounts_max);
    quota.perExec = est.accounts_cap == null ? null : count(est.accounts_cap);
    if (lastData) paintAdvice(lastData);   // 若执行体数据已到，重算建议区
  }

  YB.settingsExecutors = {
    mount: mount,
    load: load,
    apply: apply,
    applySettings: applySettings,
    save: save,
    isDirty: function () { return dirty; }
  };
})();
