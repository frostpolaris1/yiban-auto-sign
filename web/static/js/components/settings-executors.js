/* 系统设置 · 执行体与出口分区（管理端 /settings 的「执行体」tab）。

   挂载到 window.YB.settingsExecutors；classic script。

   数据源与同页其它分区**不同**：本分区只读写 `/api/scheduler/executors`（含按序号写单段与实测端点，
   仅主管理员），不来自 /api/settings（建议值一律用后端算好的 recommendation，前端不换算）。
   非主管理员：整 tab 隐藏（页面编排负责），控件仍按权限禁用 + aria-describedby 就地说明；
   禁用不是安全边界，后端 403 兜底。

   字段与语义**全部以后端下发的为准**（docs/dev/api-executors.md，字段名已冻结）：
   role / label / state / status / activity / last_seen_at 一律直接用，前端不拼、不猜、不自己算口径。

   敏感信息（出口串可能含 user:pass@）：读接口只回**描述串**（scheme://host[:port]），
   故编辑框一律留空并提示「留空 = 不修改」，保存成功后关闭弹窗并重新 GET（读回的是脱敏串，
   不能拿提交值渲染）；完整串既不入 DOM 文本与属性，也不进 title、data-* 或控制台。
   改单个执行体的出口一律走**按序号写单段**（`PUT …/workers/{index}` / `PUT …/fallback`）——
   整条回写会把别人的代理凭据清成空（静默退回直连）。

   对外面：mount(options) / load() / apply(data) / save() → Promise<boolean> / isDirty()。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ctx = { isMaster: false };
  var snap = { workers: 1 };
  var lastData = null;          // 最近一次执行体接口响应（弹窗与实测换算按需读取，不重复请求）
  var dirty = false;
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  function setText(id, text) {
    var n = $(id);
    if (n) n.textContent = text == null ? "" : String(text);
  }
  // 任何要落进 DOM 的**错误文案**先过这里：后端校验失败时会回显提交值的前 40 字符
  // （如「代理地址格式不正确: …」），而出口串可能带 user:pass@ —— 抹掉 userinfo 段，
  // 凭据不进 DOM 文本（成功路径读回的本就是脱敏描述串，无需处理）。
  function scrub(text) {
    return String(text == null ? "" : text)
      .replace(/(\b[a-z][a-z0-9+.-]*:\/\/)([^/@\s]*)@/gi, "$1***@")
      .replace(/(^|[\s(（:：])([^\s/@]+)(?::[^\s/@]*)?@/g, "$1***@");
  }
  function setTip(text, bad) { YB.setTip("set-exec-tip", scrub(text), bad); }
  function num(id, fallback) {
    var n = parseInt(($(id) || {}).value, 10);
    return isNaN(n) ? fallback : n;
  }
  function count(v) { return Number(v) || 0; }
  function attr(v) { return v == null ? "" : String(v); }

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
    var input = $("set-exec-workers");
    if (input) {
      input.disabled = !!disabled;
      if (disabled) input.setAttribute("aria-describedby", "set-exec-perm");
      else input.removeAttribute("aria-describedby");
    }
    ["set-exec-measure", "set-exec-save"].forEach(function (id) {
      var b = $(id);
      if (b) b.disabled = !!disabled;
    });
    setHidden($("set-exec-save"), disabled || !dirty);
    setHidden($("set-exec-dirty"), disabled || !dirty);
    setHidden($("set-exec-perm"), !disabled);
  }

  /* ---------------- 一览表（只读渲染，字段全部照接口） ---------------- */
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
  // 报警纪律（后端要求）：declared_not_running 只有落在**有效窗口内**才算异常——窗口外兜底
  // 进程本就退出（alive=false 是预期行为），沿用告警色等于每天非签到时段都在误报。
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
      return "成功 " + count(a.done) + " · 失败 " + count(a.failed)
        + (count(a.claimed) ? " · 进行中 " + count(a.claimed) : "");
    }
    return activities().length ? "—" : "今日尚未运行";
  }

  function paintWorkers(data) {
    var w = (data && data.workers) || {};
    var keys = w.env_keys || {};
    setText("set-exec-configured", "当前配置 " + count(w.configured || 1) + " 个并行执行体。");
    setText("set-exec-key-list", keys.list ? "对应配置项：" + keys.list : "");

    var tbody = $("set-exec-assign");
    if (!tbody) return;
    tbody.textContent = "";                       // 清空容器（不用 innerHTML）
    var rows = (w.assignments || []);
    rows.forEach(function (a) {
      var idx = count(a.index);
      tbody.appendChild(YB.el("tr", {}, [
        YB.el("td", { class: "mono", text: attr(a.label) || "并行执行体 #" + (idx + 1) }),
        YB.el("td", { text: "并行" }),
        YB.el("td", { text: a.egress || "直连（本机出口）" }),
        YB.el("td", {}, [badge(STATE_TEXT[a.state] || "—", STATE_CLASS[a.state])]),
        YB.el("td", { class: "set-exec-col-md", text: todayText(activityFor("worker", idx)) }),
        YB.el("td", {}, [rowBtn(attr(a.label) || "并行执行体 #" + (idx + 1), "worker", idx)])
      ]));
    });
    // 兜底执行体：出口与开关都能改；状态用后端算好的 status（enabled × alive 由后端合成）
    var fb = (data && data.fallback) || {};
    tbody.appendChild(YB.el("tr", {}, [
      YB.el("td", { class: "mono", text: attr(fb.label) || "兜底常驻执行体" }),
      YB.el("td", { text: "兜底" }),
      YB.el("td", { text: fb.egress || "直连（本机出口）" }),
      YB.el("td", {}, [badge(FB_TEXT[fb.status] || "—", fbClass(fb))]),
      YB.el("td", { class: "set-exec-col-md", text: todayText(activityFor("fallback", null)) }),
      YB.el("td", {}, [rowBtn(attr(fb.label) || "兜底常驻执行体", "fallback", null)])
    ]));
    // 库里的旧记录（role=unknown）：照实列出来，不猜它属于谁（后端要求：不要在前端归类）
    activities().forEach(function (a) {
      if (attr(a.role) !== "unknown") return;
      tbody.appendChild(YB.el("tr", {}, [
        YB.el("td", { text: attr(a.label) || "未标注（旧数据）" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { class: "set-exec-col-md", text: todayText(a) }),
        YB.el("td", {})
      ]));
    });
    if (!rows.length && !activities().length) {
      tbody.appendChild(YB.el("tr", {}, [
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "暂无执行体" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { class: "set-exec-col-md", text: "—" }),
        YB.el("td", {})
      ]));
    }
  }

  function rowBtn(label, role, index) {
    var btn = YB.el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "设置" });
    btn.setAttribute("aria-label", label + " 设置");
    btn.addEventListener("click", function () { openSettings(role, index); });
    return btn;
  }

  function paintFallback(data) {
    var fb = (data && data.fallback) || {};
    var badgeEl = $("set-exec-fb-badge");
    if (badgeEl) {
      badgeEl.textContent = FB_TEXT[fb.status] || "—";
      badgeEl.className = "badge dot " + fbClass(fb);
    }
    // 卡头只报异常态：off/running 是正常态（表格里已有一份，不再重复），窗口外的
    // declared_not_running 也是预期行为（见 fbAbnormal 的口径）
    var abnormal = fbAbnormal(fb);
    setText("set-exec-fb-text", fb.status === "declared_not_running"
      ? "已声明开启但没有进程在跑：宿主那条 cron 多半漏加了。"
      : (fb.status === "running_not_declared" ? "有进程在跑但不是由配置拉起的。" : ""));
    setHidden($("set-exec-fb-state"), !abnormal);
    // 报警纪律（后端要求）：只有"开了却没跑起来"**且落在有效窗口内**才报警，窗口外是预期行为
    var alarm = fb.status === "declared_not_running" && fb.in_window === true;
    var warn = $("set-exec-fb-warn");
    if (warn) {
      warn.textContent = alarm
        ? "兜底执行体声明已开启但当前没有在跑（且正在签到窗口内）：窗口内的漏签不会被补，请检查宿主 cron 是否以 --fallback 拉起。"
        : "";
      warn.hidden = !alarm;
    }
  }

  // 建议区：口径＝实测容量 × 2/3（后端算好）；没有实测就写「未实测」并隐藏建议。
  // 与「容量配额」的估算**不是同一口径**（那边是"一轮装不装得下"），故写明，不假装相等。
  function paintAdvice(data) {
    var win = (data && data.window) || {};
    var rec = (data && data.recommendation) || null;
    var measured = (data && data.measured) || null;
    setText("set-exec-window", measured
      ? "实测容量：" + count(measured.per_executor_capacity) + " 个（" + attr(measured.source) + "）"
      : "未实测：部署者尚未录入实测容量，因此不给出建议值（也不按现有账号数反算）。");
    setText("set-exec-accounts", "当前计入容量的账号数：" + count(data && data.current_accounts)
      + " 个；有效签到窗口 " + (win.start && win.end ? win.start + " ~ " + win.end : "—") + "。");
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

  /* ---------------- 行内「设置」弹窗：按序号改单个执行体的出口 ---------------- */
  // 出口编辑走**按序号写单段**（留空 = 不修改；「清除出口」= 该执行体直连/删键）。
  function openSettings(role, index) {
    if (!lastData) { setTip("数据尚未加载完成", true); return; }
    var isFb = role === "fallback";
    var w = lastData.workers || {}, fb = lastData.fallback || {};
    var row = isFb ? fb : ((w.assignments || []).filter(function (x) {
      return count(x.index) === index;
    })[0] || {});
    var acts = activityFor(role, isFb ? null : index);
    var wrap = YB.el("div");
    var grid = YB.el("div", { class: "form-grid" });
    grid.appendChild(YB.el("div", { class: "field" }, [
      YB.el("span", { class: "field-label", text: "当前出口（已脱敏）" }),
      YB.el("p", { class: "field-help", text: row.egress || "直连（本机出口）" })
    ]));
    grid.appendChild(YB.el("div", { class: "field" }, [
      YB.el("span", { class: "field-label", text: "状态" }),
      YB.el("p", { class: "field-help", text: isFb
        ? (FB_TEXT[fb.status] || "—") + (fb.in_window ? "（当前在签到窗口内）" : "（当前不在签到窗口内）")
        : (STATE_TEXT[row.state] || "—") + (row.last_seen_at ? "；最近活跃 " + row.last_seen_at : "") })
    ]));
    grid.appendChild(YB.el("div", { class: "field" }, [
      YB.el("span", { class: "field-label", text: "当日" }),
      YB.el("p", { class: "field-help", text: todayText(acts) })
    ]));
    grid.appendChild(YB.el("div", { class: "field" }, [
      YB.el("span", { class: "field-label", text: "对应配置项" }),
      YB.el("p", { class: "field-help", text: attr(isFb ? fb.env_key : (w.env_keys && w.env_keys.list)) || "—" })
    ]));
    wrap.appendChild(grid);

    var inputId = "set-exec-modal-egress";
    wrap.appendChild(YB.el("div", { class: "form-grid" }, [
      YB.el("div", { class: "field" }, [
        YB.el("label", { class: "field-label", for: inputId, text: "设置出口（留空 = 不修改）" }),
        YB.el("div", { class: "input-group" }, [
          YB.el("input", {
            class: "input", id: inputId, type: "text", autocomplete: "off", spellcheck: "false",
            placeholder: "http://user:pass@host:port"
          })
        ]),
        YB.el("p", { class: "field-help", text: isFb
          ? "兜底执行体专用出口，单段可空；「清除出口」= 不再单独配（退回单执行体出口）。"
          : "只改这一个执行体，其它段逐字保留；读接口只回脱敏串，故本框不回显原值。" })
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
        YB.el("p", { class: "field-help", id: swHelpId, text: "开启兜底执行体（只写声明开关；还需宿主 cron 以 --fallback 拉起进程才会真在跑）。" })
      ]));
    }
    // 开关这一改动是"整条端点"的 fallback_enable，与出口（单段写）分属两个请求；
    // 未改动时返回 null（不提交该字段）。
    function switchArg() {
      if (!isFb || !swInput) return null;
      return swInput.checked === (fb.enabled === true) ? null : (swInput.checked ? 1 : 0);
    }
    var tip = YB.el("p", { class: "set-tip", text: "" });
    wrap.appendChild(tip);

    var handle = YB.openModal({
      title: attr(row.label) || (isFb ? "兜底常驻执行体" : "并行执行体 #" + (index + 1)),
      subtitle: isFb ? "类型：兜底" : "类型：并行",
      body: wrap,
      actions: [
        { label: "清除出口", variant: "ghost", close: false, onClick: function () {
          // 空串 = 该段直连（契约："" 与 null 同义）；提交后仍走"重新 GET 再渲染"
          return submit(handle, tip, "", switchArg());
        } },
        { label: "保存", variant: "primary", close: false, onClick: function () {
          var v = (($(inputId) || {}).value || "").trim();
          var enableArg = switchArg();
          if (!v && enableArg == null) {
            tip.textContent = "留空 = 不修改；要改成直连请点「清除出口」。";
            tip.className = "set-tip set-bad";
            return false;
          }
          return submit(handle, tip, v || null, enableArg);
        } }
      ]
    });
    return handle;
  }

  // 提交：单段写（egress="" 表示清除/直连）+ 可选的兜底开关（整条端点的 fallback_enable）。
  // 成功后重新 GET 再渲染（契约要求），并关闭弹窗、清空输入框（完整串不驻留）。
  function submit(handle, tip, egress, enableArg) {
    if (busy) return false;
    var title = attr(handle && handle.titleEl && handle.titleEl.textContent);
    var isFb = /兜底/.test(title);
    var m = /#(\d+)/.exec(title);
    var index = m ? String(parseInt(m[1], 10) - 1) : null;
    if (!isFb && index == null) {
      tip.textContent = "无法定位该执行体，请刷新后重试。";
      tip.className = "set-tip set-bad";
      return false;
    }
    busy = true;
    tip.textContent = "提交中…";
    tip.className = "set-tip";
    var steps = [];
    if (egress != null) {
      var path = isFb ? "/api/scheduler/executors/fallback" : "/api/scheduler/executors/workers/" + index;
      steps.push(YB.api("PUT", path, { egress: egress }));
    }
    if (enableArg != null) {
      steps.push(YB.api("PUT", "/api/scheduler/executors", { fallback_enable: enableArg }));
    }
    return Promise.all(steps).then(function () {
      return load().then(function () {
        if (handle && handle.close) handle.close();
        setTip("已保存；下一轮定时任务或重启执行体/容器后生效。", false);
        return true;
      });
    }, function (e) {
      tip.textContent = scrub((e && e.message) || "保存失败，请稍后重试");
      tip.className = "set-tip set-bad";
      return false;
    }).then(function (ok) {
      busy = false;
      applyPerm();
      return ok;
    });
  }

  /* ---------------- 实测（POST /measure） ---------------- */
  // 后端会真的访问易班一次（只读链路）。按用户要求：把建议数量填进数量框，**只填不保存**。
  function measure() {
    if (busy || !ctx.isMaster) return;
    var btn = $("set-exec-measure");
    var out = $("set-exec-measure-result");
    busy = true;
    if (btn) btn.disabled = true;
    if (out) out.textContent = "实测中…（会用一个真实账号登录一次易班，只读、不签到）";
    YB.api("POST", "/api/scheduler/executors/measure", {}).then(function (d) {
      var sec = d && d.seconds != null ? Number(d.seconds) : null;
      var cap = count(d && d.per_executor_capacity);
      var perExec = count(d && d.recommended_per_executor);
      var cur = count(lastData && lastData.current_accounts);
      var need = (perExec > 0 && cur > 0) ? Math.ceil(cur / perExec) : null;
      if (out) {
        out.textContent = "实测 " + (sec == null ? "—" : sec.toFixed(2) + " 秒")
          + "（样本 " + attr(d && d.sample) + "）；单执行体容量约 " + cap
          + " 个、建议每执行体 " + perExec + " 个账号。"
          + (need == null ? "" : "按当前 " + cur + " 个账号换算：建议数量 " + need + "，已填入左侧数字框（未保存）。");
      }
      // 后端把"只覆盖窗口外最小链路、偏乐观"写进了 note（78/79 号回执确认不加 scope 字段）。
      // 页面已在按钮旁常驻同义的风险说明，故这里不再重复显示，只把它挂到结果行的 title 上备查。
      if (out && d && d.note) out.title = attr(d.note);
      if (need != null && $("set-exec-workers")) {
        $("set-exec-workers").value = String(Math.min(64, Math.max(1, need)));
        markDirty();
      }
    }, function (e) {
      if (out) out.textContent = scrub((e && e.message) || "实测失败，请稍后重试");
    }).then(function () {
      busy = false;
      if (btn) btn.disabled = !ctx.isMaster;
    });
  }

  /* ---------------- 脏状态与保存（总设置只提交数量） ---------------- */
  function changed() { return num("set-exec-workers", snap.workers) !== snap.workers; }
  function syncDirty() { if (changed()) markDirty(); else clearDirty(); }

  function save() {
    if (busy || !ctx.isMaster) return Promise.resolve(false);
    if (!changed()) {
      clearDirty();
      setTip("没有需要保存的改动", false);
      return Promise.resolve(true);
    }
    busy = true;
    setTip("保存中…", false);
    return YB.api("PUT", "/api/scheduler/executors", { workers: num("set-exec-workers", snap.workers) })
      .then(function (d) {
        // 提交后重新 GET 再渲染（契约要求），再回显 note
        return load().then(function () {
          setTip((d && d.note) || "已保存", false);
          return true;
        });
      }, function (e) {
        setTip((e && e.message) || "保存失败，请稍后重试", true);
        return false;
      }).then(function (ok) {
        busy = false;
        applyPerm();
        return ok;
      });
  }

  function apply(data) {
    lastData = data || null;
    var w = (data && data.workers) || {};
    snap.workers = count(w.configured || 1);
    if ($("set-exec-workers")) $("set-exec-workers").value = String(snap.workers);
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
    var mBtn = $("set-exec-measure");
    if (mBtn) mBtn.addEventListener("click", measure);
    var input = $("set-exec-workers");
    if (input) {
      input.addEventListener("input", syncDirty);
      input.addEventListener("change", syncDirty);
    }
    applyPerm();
  }

  YB.settingsExecutors = {
    mount: mount,
    load: load,
    apply: apply,
    save: save,
    isDirty: function () { return dirty; }
  };
})();
