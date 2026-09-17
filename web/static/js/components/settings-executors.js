/* 系统设置 · 执行体与出口分区（管理端 /settings 的「执行体」tab）。

   挂载到 window.YB.settingsExecutors；classic script。

   **数据模型＝清单**（后端 `docs/refactor/86` 交接稿）：一行一个执行体，有稳定槽位号（只增不复用）、
   类型（worker / fallback / disabled）与自己的出口。故页面上**没有"数量"输入**——行数＝清单长度，
   增行用「添加执行体」、删行在行内弹窗里。渲染一律按接口的 `executors[]`（`slot` 升序），
   **不假设槽位号连续**（删行后新建的行会跳过领取历史里用过的号）。

   写操作全部走**行接口**（立即写入 `.env`，没有"整页保存"这一步）：
     POST   /api/scheduler/executors/rows             追加一行
     PUT    /api/scheduler/executors/rows/{slot}      改一行的类型 / 出口（只动这一行）
     DELETE /api/scheduler/executors/rows/{slot}      删一行
   旧三键（YIBAN_WORKERS / YIBAN_PROXY_LIST / YIBAN_PROXY_FALLBACK）后端已一次性迁移进清单，
   页面**不再写它们**（写了会和清单打架）。兜底的「开关」（YIBAN_FALLBACK_ENABLE）与
   「兜底那一行」是两件事，开关仍走 `PUT /api/scheduler/executors`。

   非主管理员：整 tab 隐藏（页面编排负责），控件按权限禁用 + 就地说明；**禁用不是安全边界**，
   后端对每个端点独立判主管理员（403），写接口另需 CSRF。

   字段与语义**全部以后端下发的为准**（docs/dev/api-executors.md，字段名已冻结）：
   type / label / state / status / activity / egress 一律直接用，前端不拼、不猜、不自己算口径；
   `disabled` 与 `fallback` 行的 `state` 是 `null`，照实渲染（分类只看 `type`）。

   敏感信息（出口串可能含 user:pass@）：读接口只回**描述串**（scheme://host[:port]），
   故编辑框一律留空并提示「留空 = 不修改」，写成功后重新 GET 再渲染（读回的是脱敏串，
   不能拿提交值渲染）；完整串既不入 DOM 文本与属性，也不进 title、data-* 或控制台。
   行内写入**绝不影响其他行**（后端按段写、其余行逐字保留）——见 78 号验收②。

   对外面：mount(options) / load() / apply(data) / save() / isDirty()。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ctx = { isMaster: false };
  var lastData = null;          // 最近一次执行体接口响应（弹窗与实测换算按需读取，不重复请求）
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  function setText(id, text) {
    var n = $(id);
    if (n) n.textContent = text == null ? "" : String(text);
  }
  // 错误文案直接显示：后端已在源头抹掉 userinfo（yiban/masking.mask_url_userinfo，见 81/82 号
  // 文档），保证 400 的 error 不含凭据——前端不再自己脱敏，避免两处口径分叉。
  function setTip(text, bad) { YB.setTip("set-exec-tip", text, bad); }
  function count(v) { return Number(v) || 0; }
  function attr(v) { return v == null ? "" : String(v); }
  function workers() {
    return executors().filter(function (r) { return attr(r.type) === "worker"; }).length;
  }

  function applyPerm() {
    var disabled = !ctx.isMaster;
    ["set-exec-row-add", "set-exec-advice-open"].forEach(function (id) {
      var b = $(id);
      if (b) b.disabled = !!disabled;
    });
    setHidden($("set-exec-perm"), !disabled);
  }

  /* ---------------- 文案映射（键都来自后端字段，前端只做中文） ---------------- */
  var TYPE_TEXT = { worker: "并行", fallback: "兜底", disabled: "停用" };
  var TYPE_OPTIONS = [
    { v: "worker", t: "并行执行体（拉起并参与分配）" },
    { v: "fallback", t: "兜底常驻执行体（窗口内补签，最多一行）" },
    { v: "disabled", t: "停用（保留出口与槽位，不拉起、不计入建议值）" }
  ];
  var STATE_TEXT = {
    running: "正在跑本轮",
    finished: "本轮已跑完",
    idle: "今天还没跑",
    stale: "可能被中断（无收尾记录）"
  };
  var STATE_CLASS = {
    running: "badge--ok",
    finished: "badge--muted",
    idle: "badge--muted",
    stale: "badge--bad"
  };
  var FB_TEXT = {
    off: "未开启兜底",
    running: "正在运行",
    declared_not_running: "已开启但没跑起来",
    running_not_declared: "有进程在跑（非配置拉起）"
  };
  var FB_CLASS = {
    off: "badge--muted",
    running: "badge--ok",
    declared_not_running: "badge--bad",
    running_not_declared: "badge--info"
  };
  // 报警纪律（后端要求）：declared_not_running 只有落在**本应运行时段内**才算异常——窗口外
  // 兜底进程本就退出（alive=false 是预期行为），沿用告警色等于每天非签到时段都在误报。
  function fbAbnormal(fb) {
    return fb.status === "running_not_declared"
      || (fb.status === "declared_not_running" && fb.in_window === true);
  }
  function fbClass(fb) {
    if (fb.status === "declared_not_running" && fb.in_window !== true) return "badge--muted";
    return FB_CLASS[fb.status] || "badge--muted";
  }
  function badge(text, cls) {
    return YB.el("span", { class: "badge dot " + (cls || "badge--muted"), text: text });
  }
  function activities() {
    return (lastData && lastData.activity && lastData.activity.by_executor) || [];
  }
  // 当日计数：按 role+index 命中该执行体；命中不到时，数组为空即"今日尚未运行"（后端约定的信号）
  function activityFor(role, index) {
    var acts = activities();
    for (var i = 0; i < acts.length; i++) {
      var a = acts[i];
      if (attr(a.role) === role && (index == null || count(a.index) === index)) return a;
    }
    return null;
  }
  function todayText(a) {
    if (a) {
      return "当日 成功 " + count(a.done) + " · 失败 " + count(a.failed)
        + (count(a.claimed) ? " · 进行中 " + count(a.claimed) : "");
    }
    return activities().length ? "当日 —" : "今日尚未运行";
  }
  function executors() { return (lastData && lastData.executors) || []; }
  // 配置项键名由接口给（workers.env_keys.manifest），槽位下标是后端审计与 .env 里的写法
  function manifestKey(slot) {
    var keys = (lastData && lastData.workers && lastData.workers.env_keys) || {};
    return attr(keys.manifest || "") + "[" + count(slot) + "]";
  }

  /* ---------------- 清单：摘要 + 一览表 ---------------- */
  function rowsSummary() {
    var rows = executors();
    var n = { worker: 0, fallback: 0, disabled: 0 };
    rows.forEach(function (r) { n[attr(r.type)] = (n[attr(r.type)] || 0) + 1; });
    var parts = ["并行 " + n.worker, "兜底 " + n.fallback];
    if (n.disabled) parts.push("停用 " + n.disabled);
    return "当前清单 " + rows.length + " 行：" + parts.join(" · ");
  }

  // 状态与当日合并成一列（用户 2026-09-17：两者是同一件事的两面，各占一列只是白占宽度）
  function stateCell(row) {
    var type = attr(row.type);
    var cell = YB.el("td", { class: "set-exec-state" });
    if (type === "disabled") {
      cell.appendChild(YB.el("span", { class: "set-exec-off", text: "停用中（不拉起、不计入建议值）" }));
      return cell;
    }
    if (type === "fallback") {
      var fb = (lastData && lastData.fallback) || {};
      cell.appendChild(badge(FB_TEXT[fb.status] || "—", fbClass(fb)));
      cell.appendChild(YB.el("span", { class: "set-exec-daily", text: todayText(activityFor("fallback", null)) }));
      return cell;
    }
    cell.appendChild(badge(STATE_TEXT[row.state] || "—", STATE_CLASS[row.state]));
    cell.appendChild(YB.el("span", { class: "set-exec-daily", text: todayText(activityFor("worker", count(row.slot))) }));
    return cell;
  }

  function rowBtn(row) {
    var label = attr(row.label) || "执行体";
    var btn = YB.el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "设置" });
    btn.setAttribute("aria-label", label + " 设置（槽位 " + count(row.slot) + "）");
    btn.addEventListener("click", function () { openRow(row); });
    return btn;
  }

  function paintRows() {
    setText("set-exec-rows-summary", rowsSummary());
    var tbody = $("set-exec-assign");
    if (!tbody) return;
    tbody.textContent = "";                       // 清空容器（不用 innerHTML）
    var rows = executors().slice().sort(function (a, b) { return count(a.slot) - count(b.slot); });
    rows.forEach(function (r) {
      var type = attr(r.type);
      tbody.appendChild(YB.el("tr", { class: type === "disabled" ? "set-exec-row-off" : "" }, [
        YB.el("td", {}, [
          YB.el("span", { class: "mono", text: attr(r.label) || TYPE_TEXT[type] || "—" }),
          // 停用行的标签口径就是「已停用」（后端冻结、不含槽位），多个停用行只靠标签区分不了，
          // 故这里补一个槽位号——槽位是后端给的稳定身份，不是我拼的文案。
          type === "disabled" ? YB.el("span", { class: "set-exec-slot", text: "（槽位 " + count(r.slot) + "）" }) : null
        ]),
        YB.el("td", { text: TYPE_TEXT[type] || "—" }),
        YB.el("td", { text: r.egress || "直连（本机出口）" }),
        stateCell(r),
        YB.el("td", {}, [rowBtn(r)])
      ]));
    });
    // 库里的旧记录（role=unknown）：照实列出来，不猜它属于谁（后端要求：不要在前端归类）
    activities().forEach(function (a) {
      if (attr(a.role) !== "unknown") return;
      tbody.appendChild(YB.el("tr", {}, [
        YB.el("td", { text: attr(a.label) || "未标注（旧数据）" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { class: "set-exec-state" }, [
          YB.el("span", { class: "set-exec-daily", text: todayText(a) })
        ]),
        YB.el("td", {})
      ]));
    });
    if (!rows.length && !activities().length) {
      tbody.appendChild(YB.el("tr", {}, [
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "清单为空：点上方「添加执行体」加一行" }),
        YB.el("td", { class: "set-exec-state" }, [YB.el("span", { class: "set-exec-daily", text: "—" })]),
        YB.el("td", {})
      ]));
    }
  }

  function paintFallback(data) {
    var fb = (data && data.fallback) || {};
    var badgeEl = $("set-exec-fb-badge");
    if (badgeEl) {
      badgeEl.textContent = FB_TEXT[fb.status] || "—";
      badgeEl.className = "badge dot " + fbClass(fb);
    }
    // 卡头只留短标签；"多半漏加了 cron"那类处置说明交给下面那条**窗口内才出现**的告警
    // （#set-exec-fb-warn），避免同一件事在卡头与告警里各说一遍（提醒不占页面）。
    setText("set-exec-fb-text", fb.status === "declared_not_running"
      ? "已声明开启但没有进程在跑。"
      : (fb.status === "running_not_declared" ? "有进程在跑，但不是由配置拉起的。" : ""));
    setHidden($("set-exec-fb-state"), !fbAbnormal(fb));
    // 报警纪律（后端要求）：只有"开了却没跑起来"**且落在本应运行时段内**才报警
    var alarm = fb.status === "declared_not_running" && fb.in_window === true;
    var warn = $("set-exec-fb-warn");
    if (warn) {
      warn.textContent = alarm
        ? "兜底执行体声明已开启但当前没有在跑（且正在签到时段内）：窗口内的漏签不会被补，"
          + "请检查宿主 cron（或容器调度器）是否以 --fallback 拉起。"
        : "";
      warn.hidden = !alarm;
    }
  }

  /* ---------------- 写入：行增删 / 改类型 / 改出口（都立即落盘） ---------------- */
  function note(d) { return attr(d && d.note) || "已写入配置"; }
  function failTip(e, what) { setTip((e && e.message) || (what + "失败，请稍后重试"), true); }

  function withBusy(fn) {
    if (busy) return Promise.resolve(false);
    busy = true;
    return Promise.resolve().then(fn).then(function (ok) {
      busy = false; applyPerm(); return ok;
    }, function () {
      busy = false; applyPerm(); return false;
    });
  }

  function addRow() {
    if (!ctx.isMaster) return;
    withBusy(function () {
      setTip("添加中…", false);
      return YB.api("POST", "/api/scheduler/executors/rows", { type: "worker" }).then(function (d) {
        return load().then(function () {
          setTip("已添加「并行执行体 #" + (count(d && d.slot) + 1) + "」（默认直连）：" + note(d)
            + "。想给它单独出口，点那一行的「设置」。", false);
          return true;
        });
      }, function (e) { failTip(e, "添加"); });
    });
  }

  function putRow(slot, payload, okText) {
    return withBusy(function () {
      setTip("提交中…", false);
      return YB.api("PUT", "/api/scheduler/executors/rows/" + slot, payload).then(function (d) {
        return load().then(function () {
          setTip(okText + "：" + note(d), false);
          return true;
        });
      }, function (e) { failTip(e, "保存"); });
    });
  }

  function deleteRow(row, handle) {
    var slot = count(row.slot);
    var label = attr(row.label) || ("槽位 " + slot);
    YB.confirmDialog({
      title: "删除这一行？",
      body: label + "（槽位 " + slot + "）会从清单里移除，它当前的出口配置一并删除。"
        + "只是暂时不用的话建议改成「停用」——停用保留出口与槽位号，删除不保留。",
      confirmText: "删除",
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      if (handle && handle.close) handle.close();
      withBusy(function () {
        setTip("删除中…", false);
        return YB.api("DELETE", "/api/scheduler/executors/rows/" + slot).then(function (d) {
          return load().then(function () {
            setTip("已删除 " + label + "：" + note(d), false);
            return true;
          });
        }, function (e) { failTip(e, "删除"); });
      });
    });
  }

  /* ---------------- 行内弹窗：类型 + 出口（低频与破坏性动作降到正文里） ---------------- */
  // 类型用自研选择控件（原生 select 的弹出层不可控，项目约定全部自研，见 shared-frontend-components）
  function typeField(current) {
    var root = YB.el("div", { class: "select-field", "data-select-field": "set-exec-row-type" });
    root.appendChild(YB.el("input", { type: "hidden", id: "set-exec-row-type", value: attr(current) }));
    var menu = YB.el("div", { class: "select-menu", role: "listbox", hidden: true });
    TYPE_OPTIONS.forEach(function (o) {
      menu.appendChild(YB.el("button", {
        type: "button", class: "select-option", role: "option", "data-v": o.v, text: o.t
      }));
    });
    root.appendChild(menu);
    return YB.el("div", { class: "field" }, [
      YB.el("span", { class: "field-label" }, [
        YB.el("span", { text: "类型" }),
        infoTip("类型与出口的改法", ROW_HELP)
      ]),
      root
    ]);
  }

  // 弹窗内的 info 浮层：与模板的 {% call info(label, id) %} 同构（.info-tip + .info-pop，
  // hover/focus 双触发，CSS 里 pointer-events:none）。`up` = 向上展开——弹窗面板是
  // overflow:hidden，靠近底部的浮层向下开会被裁掉（实测）。
  var popUid = 0;
  function infoTip(label, text, up) {
    popUid += 1;
    var popId = "set-exec-pop-" + popUid;
    var btn = YB.el("button", { type: "button", class: "info-tip" });
    btn.setAttribute("aria-label", label);
    btn.setAttribute("aria-describedby", popId);
    btn.appendChild(YB.iconEl("info"));
    btn.appendChild(YB.el("span", {
      class: "info-pop" + (up ? " info-pop--up" : ""), id: popId, role: "tooltip", text: text
    }));
    return btn;
  }

  // 文本级动作（与「保存」分属不同视觉层级）；extraClass 给破坏性动作上危险色
  var ROW_HELP = "类型：并行＝会拉起并计入建议值；兜底＝窗口内补签，最多一行、不计入；"
    + "停用＝保留出口与槽位、不拉起、不计入（想留位置请用停用而不是删除）。"
    + "出口：读接口只回脱敏描述串（不含账号密码），本框不回显原值，留空＝不修改；"
    + "改成直连请用下面的「清除出口」。写入只动这一行，其余行（含停用行的出口）逐字保留；"
    + "改动即时写入配置，下一轮定时任务或重启执行体/容器后生效。"
    + "「删除这一行」会连同它的出口配置一起移除且不保留槽位号。";

  function linkBtn(text, onClick, extraClass) {
    var b = YB.el("button", { type: "button", class: "linklike " + (extraClass || ""), text: text });
    b.addEventListener("click", onClick);
    return b;
  }

  function openRow(row) {
    if (!lastData) { setTip("数据尚未加载完成", true); return null; }
    var slot = count(row.slot);
    var type = attr(row.type);
    var isFb = type === "fallback";
    var fb = (lastData.fallback) || {};
    var acts = activityFor(isFb ? "fallback" : "worker", isFb ? null : slot);

    var wrap = YB.el("div");
    wrap.appendChild(typeField(type));

    // 「当前出口」与「对应配置项」合并成一行（用户 2026-09-17：这两条本来在说同一件事）
    wrap.appendChild(YB.el("div", { class: "field" }, [
      YB.el("span", { class: "field-label", text: "当前出口（已脱敏）" }),
      YB.el("p", { class: "field-help", text: (row.egress || "直连（本机出口）") + "｜配置项 " + manifestKey(slot) })
    ]));

    var inputId = "set-exec-modal-egress";
    wrap.appendChild(YB.el("div", { class: "field" }, [
      YB.el("label", { class: "field-label", for: inputId, text: "设置出口（留空 = 不修改）" }),
      YB.el("div", { class: "input-group" }, [
        YB.el("input", {
          class: "input", id: inputId, type: "text", autocomplete: "off", spellcheck: "false",
          placeholder: "http://user:pass@host:port"
        })
      ])
    ]));

    var swInput = null;
    if (isFb) {
      // 开关与模板里的同类控件同构：label 内补 sr-only 文本给读屏，语义说明用
      // aria-describedby 程序化关联（与 #set-exec-perm 的做法一致）
      swInput = YB.el("input", { type: "checkbox" });
      if (fb.enabled === true) swInput.checked = true;
      var swHelpId = "set-exec-modal-fb-help";
      swInput.setAttribute("aria-describedby", swHelpId);
      wrap.appendChild(YB.el("div", { class: "field" }, [
        YB.el("label", { class: "switch", title: "开启兜底执行体" }, [
          swInput, YB.el("span", { class: "track", "aria-hidden": "true" }),
          YB.el("span", { class: "sr-only", text: "开启兜底执行体" })
        ]),
        YB.el("p", { class: "field-help", id: swHelpId, text: "要不要拉起兜底进程（只写声明开关；还需宿主 cron 或容器调度器以 --fallback 拉起进程才会真在跑）。" })
      ]));
    }
    if (type !== "disabled") {
      wrap.appendChild(YB.el("p", { class: "set-summary", text: isFb
        ? "存活：" + (FB_TEXT[fb.status] || "—") + (fb.in_window ? "（当前在本应运行时段内）" : "（当前不在本应运行时段内）")
          + "　" + todayText(acts)
        : "存活：" + (STATE_TEXT[row.state] || "—") + (row.last_seen_at ? "；最近活跃 " + attr(row.last_seen_at) : "")
          + "　" + todayText(acts) }));
    }

    var tip = YB.el("p", { class: "set-tip", text: "" });

    // 低频且破坏性的动作降级到正文里（用户 2026-09-17：「清除出口」与「保存」不是一个视觉层级）
    wrap.appendChild(YB.el("p", { class: "set-exec-subactions" }, [
      linkBtn("清除出口（改为直连）", function () {
        YB.confirmDialog({
          title: "清除这一行的出口？",
          body: "该行会改成直连（本机出口），原出口配置从配置项里删掉，不可撤销。",
          confirmText: "清除",
          danger: true
        }).then(function (ok) {
          if (!ok) return;
          if (handle && handle.close) handle.close();
          putRow(slot, { proxy: "" }, "已清除出口");
        });
      }),
      YB.el("span", { class: "set-exec-subactions__sep", text: "·" }),
      linkBtn("删除这一行", function () { deleteRow(row, handle); }, "set-exec-danger")
    ]));
    wrap.appendChild(tip);

    function switchArg() {
      if (!isFb || !swInput) return null;
      return swInput.checked === (fb.enabled === true) ? null : (swInput.checked ? 1 : 0);
    }

    function save() {
      if (busy) return false;
      var newType = YB.selectField ? attr(YB.selectField.read("set-exec-row-type")) : type;
      var egress = (($(inputId) || {}).value || "").trim();
      var enableArg = switchArg();
      var changedType = !!newType && newType !== type;
      if (!changedType && !egress && enableArg == null) {
        tip.textContent = "没有需要保存的改动（留空 = 不修改出口；改成直连请点上面的「清除出口」）。";
        tip.className = "set-tip set-bad";
        return false;
      }
      busy = true;
      tip.textContent = "提交中…";
      tip.className = "set-tip";
      var steps = [];
      if (changedType || egress) {
        var payload = {};
        if (changedType) payload.type = newType;
        if (egress) payload.proxy = egress;      // 留空＝不改出口（契约里空串＝直连，故绝不发空串）
        steps.push(YB.api("PUT", "/api/scheduler/executors/rows/" + slot, payload));
      }
      if (enableArg != null) {
        steps.push(YB.api("PUT", "/api/scheduler/executors", { fallback_enable: enableArg }));
      }
      return Promise.all(steps).then(function (res) {
        return load().then(function () {
          if (handle && handle.close) handle.close();
          setTip(note(res && res[0]), false);
          return true;
        });
      }, function (e) {
        busy = false;
        tip.textContent = (e && e.message) || "保存失败，请稍后重试";
        tip.className = "set-tip set-bad";
        return false;
      }).then(function (ok) {
        busy = false; applyPerm(); return ok;
      });
    }

    var handle = YB.openModal({
      title: attr(row.label) || "执行体",
      subtitle: "槽位 " + slot,
      body: wrap,
      actions: [
        { label: "取消", variant: "ghost" },
        { label: "保存", variant: "primary", close: false, onClick: function () { return save(); } }
      ]
    });
    if (YB.selectField) YB.selectField.mount();      // 弹窗内的自研选择控件此刻才进 DOM
    return handle;
  }

  /* ---------------- 建议：页面一行摘要 + 弹窗明细 ---------------- */
  // 口径＝实测容量 × 2/3（后端算好）；没有实测就写「未实测」并隐藏建议。
  // 与「容量配额」的估算**不是同一口径**（那边是"一轮装不装得下"），故写明，不假装相等。
  // 页面只留一行摘要；明细、实测入口与两条风险说明都在「容量实测与建议」弹窗里
  // （用户 2026-09-17：低频功能与提醒收进弹窗，节约页面空间）——弹窗元素沿用同一批 id，
  // 所以下面这些 setText 会同时刷页面摘要与（打开着的）弹窗明细。
  function adviceSummary(data) {
    var win = (data && data.window) || {};
    var rec = (data && data.recommendation) || null;
    var measured = (data && data.measured) || null;
    if (!measured) return "未实测：点「查看与实测」做一次实测，或由部署者录入实测容量，才会给出建议执行体数。";
    // 尾巴上的「建议值，非程序上限」是**口径**（清单能加到 63 行，量与它无关）——复审判定
    // 「×2/3、只是建议不是上限」若只活在弹窗黄条里就收得过深，故在摘要行留这 8 字。
    return "建议并行执行体数：" + (rec ? count(rec.executors_needed) + " 个" : "—")
      + "（每个约 " + (rec ? count(rec.per_executor_accounts) : "—") + " 个账号，含慢账号余量）"
      + " · 实测容量 " + count(measured.per_executor_capacity) + " 个"
      + " · 计入容量 " + count(data && data.current_accounts) + " 个账号"
      + (win.start && win.end ? " · 窗口 " + win.start + " ~ " + win.end : "")
      + "（建议值，非程序上限）";
  }

  function paintAdvice(data) {
    var win = (data && data.window) || {};
    var rec = (data && data.recommendation) || null;
    var measured = (data && data.measured) || null;
    setText("set-exec-advice-line", adviceSummary(data));
    // 以下四个 id 只存在于弹窗里：弹窗没开时 setText/setHidden 静默跳过（打开时 openAdvice 会再刷一遍）
    setText("set-exec-window", measured
      ? "实测容量：" + count(measured.per_executor_capacity) + " 个（" + attr(measured.source) + "）"
      : "未实测：部署者尚未录入实测容量，因此不给出建议值（也不按现有账号数反算）。");
    setText("set-exec-accounts", "当前计入容量的账号数：" + count(data && data.current_accounts)
      + " 个；有效签到窗口 " + (win.start && win.end ? win.start + " ~ " + win.end : "—")
      + "；建议值只数清单里的「并行」行（现在 " + workers() + " 行）。");
    if (rec) {
      setText("set-exec-advice-nums", "建议执行体数：" + count(rec.executors_needed)
        + " 个（每个执行体约 " + count(rec.per_executor_accounts) + " 个账号，含慢账号余量）。");
      setText("set-exec-advice-note", attr(rec.note));
      setHidden($("set-exec-advice-nums"), false);
      setHidden($("set-exec-advice-note"), !rec.note);
    } else {
      setText("set-exec-advice-nums", "");
      setText("set-exec-advice-note", "");
      setHidden($("set-exec-advice-nums"), true);
      setHidden($("set-exec-advice-note"), true);
    }
  }

  /* ---------------- 「容量实测与建议」弹窗（低频操作 + 口径提醒的归属处） ---------------- */
  // 后端要求两条风险必须写进页面（77/73 号）：默认拿列表第一个可签账号；只覆盖窗口外最小链路。
  // 它们与实测按钮放在同一个弹窗里——在"要动手的那一刻"给出，比常驻在页面上更省空间也更贴题。
  var MEASURE_INTRO = "点「测试」会用一个真实账号走一次只读链路（登录 + 拉任务，不提交签到）"
    + "测出耗时，并据此给出建议执行体数（只提示，不改清单）。";
  var MEASURE_RISK = "说明：默认取账号列表里第一个可签账号 —— 通常是某位真实用户的账号，"
    + "且顺序稳定、每次都是同一个；它只覆盖窗口外的最小链路（登录 + 拉任务，5 次请求），"
    + "比真实签到（含定位与提交，6 次请求）偏乐观。正式定档请以测试机基准为准，两种数字不要混用；"
    + "窗口内点不了（后端返回 409），且两次实测之间有冷却时间。";

  function openAdvice() {
    if (!lastData) { setTip("数据尚未加载完成", true); return null; }
    var wrap = YB.el("div");
    wrap.appendChild(YB.el("p", { class: "field-help", id: "set-exec-window" }));
    wrap.appendChild(YB.el("p", { class: "field-help", id: "set-exec-accounts" }));
    wrap.appendChild(YB.el("p", { class: "field-help", id: "set-exec-advice-nums" }));
    wrap.appendChild(YB.el("p", { class: "set-warn", id: "set-exec-advice-note", hidden: true }));
    // 说明与风险都在按钮旁的 ⓘ 里（用户 2026-09-17：弹窗内文案也靠 info 折叠）；
    // 浮层向上展开——按钮在弹窗靠底部，向下开会被 .pm-panel 的 overflow:hidden 裁掉。
    var mBtn = YB.el("button", { type: "button", class: "btn btn--ghost btn--sm", id: "set-exec-measure", text: "测试" });
    mBtn.disabled = !ctx.isMaster;                  // 禁用不是安全边界，后端 403 兜底
    mBtn.addEventListener("click", measure);
    wrap.appendChild(YB.el("p", { class: "set-exec-actions" }, [
      mBtn,
      infoTip("实测说明与风险", MEASURE_INTRO + " " + MEASURE_RISK, true)
    ]));
    wrap.appendChild(YB.el("p", { class: "field-help", id: "set-exec-measure-result", role: "status" }));
    var handle = YB.openModal({
      title: "容量实测与建议",
      subtitle: "实测只给建议，不改清单",
      body: wrap,
      actions: [{ label: "关闭", variant: "ghost" }]
    });
    paintAdvice(lastData);                          // 打开时把明细填上（元素此刻才存在）
    return handle;
  }

  /* ---------------- 实测（POST /measure） ---------------- */
  // 后端会真的访问易班一次（只读链路）。结果行的三态样式：中性（说明/进行中）/ 失败上色 / 成功。
  function measureOut(text, bad) {
    var out = $("set-exec-measure-result");
    if (!out) return;
    out.textContent = text;
    out.className = bad ? "field-help set-bad" : "field-help";
  }
  // 429 的 body 带 next_allowed_in（秒）：把"还要等多久"翻成人话，别只说"冷却中"
  function cooldownText(e) {
    var sec = e && e.data && Number(e.data.next_allowed_in);
    if (!sec || sec <= 0) return "";
    var min = Math.ceil(sec / 60);
    return "约 " + (min > 1 ? min + " 分钟" : Math.max(1, Math.round(sec)) + " 秒") + "后可再测";
  }
  function measure() {
    if (busy || !ctx.isMaster) return;
    var btn = $("set-exec-measure");
    var out = $("set-exec-measure-result");
    busy = true;
    if (btn) btn.disabled = true;
    measureOut("实测中…（会用一个真实账号登录一次易班，只读、不签到）", false);
    YB.api("POST", "/api/scheduler/executors/measure", {}).then(function (d) {
      var sec = d && d.seconds != null ? Number(d.seconds) : null;
      var cap = count(d && d.per_executor_capacity);
      var perExec = count(d && d.recommended_per_executor);
      var cur = count(lastData && lastData.current_accounts);
      var need = (perExec > 0 && cur > 0) ? Math.ceil(cur / perExec) : null;
      measureOut("实测 " + (sec == null ? "—" : sec.toFixed(2) + " 秒")
        + "（样本 " + attr(d && d.sample) + "）；单执行体容量约 " + cap
        + " 个、建议每执行体 " + perExec + " 个账号。"
        + (need == null ? "" : "按当前 " + cur + " 个账号换算：建议 " + need
          + " 行「并行」（清单现在 " + workers() + " 行，未自动改动——增减行请用「添加执行体」与行内「设置」）。"), false);
      // 后端把"只覆盖窗口外最小链路、偏乐观"写进了 note（78/79 号回执确认不加 scope 字段）。
      // 同一句话已经以静态文案写在按钮下方（MEASURE_RISK），故这里不再重复显示，
      // 只把后端原文挂到结果行的 title 上备查（便于对照后端口径变更）。
      if (out && d && d.note) out.title = attr(d.note);
    }, function (e) {
      var cool = cooldownText(e);
      measureOut(((e && e.message) || "实测失败，请稍后重试") + (cool ? "（" + cool + "）" : ""), true);
    }).then(function () {
      busy = false;
      if (btn) btn.disabled = !ctx.isMaster;
    });
  }

  function apply(data) {
    lastData = data || null;
    paintRows();
    paintFallback(data);
    paintAdvice(data);
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
    var addBtn = $("set-exec-row-add");
    if (addBtn) addBtn.addEventListener("click", addRow);
    var aBtn = $("set-exec-advice-open");
    if (aBtn) aBtn.addEventListener("click", openAdvice);
    applyPerm();
  }

  YB.settingsExecutors = {
    mount: mount,
    load: load,
    apply: apply,
    // 清单模型下每个写操作**立即落盘**，没有"待保存的整页改动"——保留下面两个方法只为满足
    // 页面统一的「未保存改动守卫」接口（见 pages/work_settings.js 的 stores()）。
    save: function () { return Promise.resolve(true); },
    isDirty: function () { return false; }
  };
})();
