/* 系统设置 · 执行体与出口分区（管理端 /work/settings 的「执行体」tab）。

   挂载到 window.YB.settingsExecutors；classic script。

   **数据模型＝清单**（后端 `docs/refactor/86` 交接稿；该目录未纳入版本控制，接口形态以
   `docs/dev/api-executors.md` 的 `executors[]` 一节为准）：一行一个执行体，有稳定槽位号（只增不复用）、
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
  // 操作反馈走横幅（用户 2026-09-17：「已删除 并行执行体 #8：已写入配置…」做成横幅形态）。
  // 错误文案直接显示：后端已在源头抹掉 userinfo（yiban/masking.mask_url_userinfo，见 81/82 号
  // 文档），保证 400 的 error 不含凭据——前端不再自己脱敏，避免两处口径分叉。
  var BANNER_ICON = { success: "circle-check", danger: "circle-x", info: "info" };
  function setTip(text, bad) { banner(text, bad ? "danger" : "success"); }
  function banner(text, kind) {
    var box = $("set-exec-banner");
    if (!box) return;
    var ico = $("set-exec-banner-ico");
    var body = $("set-exec-banner-body");
    if (ico) {
      ico.textContent = "";
      ico.appendChild(YB.iconEl(BANNER_ICON[kind] || "info"));
    }
    if (body) body.textContent = text == null ? "" : String(text);
    box.className = "alert " + (kind || "info");
    box.hidden = !text;
  }
  function count(v) { return Number(v) || 0; }
  function attr(v) { return v == null ? "" : String(v); }

  // 本分区=整 tab 仅主管理员可见（页面编排负责隐藏 tab 与面板），故页面上没有"控件已禁用"的说明条；
  // 禁用仍逐控件设一遍：隐藏是界面口径，禁用是纵深（真正的边界在后端 403）。
  function applyPerm() {
    var b = $("set-exec-row-add");
    if (b) b.disabled = !ctx.isMaster;
  }

  /* ---------------- 文案映射（键都来自后端字段，前端只做中文） ---------------- */
  // 兜底那一行的显示名由后端定（`yiban/egress.py::role_label`，2026-09-17 起是「故障转移」，
  // 同时作用于 executors[] 的 label、fallback.label 与账号页「上次实领」的角色列）。
  // 故**不再自己拼「兜底 / 故障转移」这种双写**——类型列用同一个词，行名一律用接口给的 label/name。
  // `type` 的取值仍是 fallback（契约未动），这里只映射中文。
  var TYPE_TEXT = { worker: "并行", fallback: "故障转移", disabled: "停用" };
  var STATE_TEXT = {
    running: "正在跑本轮",
    finished: "本轮已跑完",
    idle: "今天还没跑",
    // 徽标只写短名：完整口径（"有开始、无收尾记录"）写在卡头 ⓘ 里。
    // 早先是「可能被中断（无收尾记录）」，实测 158×21.4，把状态列固有宽撑到 186px——
    // 叠加"状态/当日"拆列后整表固有宽 937px > 1024 档的卡片内容宽 842px，操作列被推出可视区。
    stale: "可能被中断"
  };
  // 状态胶囊配色（用户 2026-09-17）：正在跑=绿、本轮已跑完=**蓝**、今天还没跑=灰、可能被中断=红；
  // 停用行在状态栏给**灰底胶囊**（原来只有一行灰字）。
  var STATE_CLASS = {
    running: "badge--ok",
    finished: "badge--info",
    idle: "badge--muted",
    stale: "badge--bad"
  };
  // 故障转移那行的状态一律**以配置开关为主语**报，别把"开关没开"与"没跑起来"说成一件事——
  // 旧文案「未开启故障转移」既能读成"这个功能没启用"，也能读成"这个执行体还没启动"，第一次看到
  // 分不清该去哪一栏找原因。故：off = 配置里没启用（开关关着）；declared_not_running = 声明启用但
  // 当前没有进程（窗口外属正常，窗口内才算异常，见 fbAbnormal）；running_not_declared = 反过来。
  var FB_TEXT = {
    off: "未启用",
    running: "运行中",
    declared_not_running: "已启用·未运行",
    running_not_declared: "运行中·配置未启用"
  };
  var FB_CLASS = {
    off: "badge--muted",
    running: "badge--ok",
    declared_not_running: "badge--bad",
    running_not_declared: "badge--info"
  };
  // 报警纪律（后端要求）：declared_not_running 只有落在**本应运行时段内**才算异常——窗口外
  // 故障转移进程本就退出（alive=false 是预期行为），沿用告警色等于每天非签到时段都在误报。
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
  // 当日计数正文（该列标题已是「当日」，正文不再重复"当日"二字）
  function dailyText(a) {
    return "成功 " + count(a.done) + " · 失败 " + count(a.failed)
      + (count(a.claimed) ? " · 进行中 " + count(a.claimed) : "");
  }
  function executors() { return (lastData && lastData.executors) || []; }
  // 行名：**用户自定义名优先**（后端 name 字段），否则用后端给的标签（label 口径仍以后端为准）。
  // 故障转移行的名称由后端定、固定，不接受自定义名（用户 2026-09-17）。
  function rowName(row) {
    if (attr(row.type) !== "fallback" && attr(row.name)) return attr(row.name);
    return attr(row.label) || TYPE_TEXT[attr(row.type)] || "执行体";
  }
  // 消息里的标识：停用行的后端标签就是「已停用」（不含槽位），直接拿它拼句子会得到
  // "已启用 已停用"这种自相矛盾的文案，故停用行补槽位号（与表格里那格一致）。
  function rowTitle(row) {
    if (attr(row.type) === "disabled") return "执行体（槽位 " + count(row.slot) + "）";
    return rowName(row);
  }
  // 出口的显示前缀：接口对直连行回中文「直连（本机出口）」、对代理行回**裸** scheme://host[:port]，
  // 两行并排时口径不对仗，故代理串在渲染层补「代理 」前缀。接口字段与后端 describe() 都不同步改。
  var EGRESS_DIRECT = "直连（本机出口）";
  function egressText(v) {
    var s = attr(v);
    return (!s || s === EGRESS_DIRECT) ? EGRESS_DIRECT : "代理 " + s;
  }
  // 配置项键名由接口给（workers.env_keys.manifest），槽位下标是后端审计与 .env 里的写法
  function manifestKey(slot) {
    var keys = (lastData && lastData.workers && lastData.workers.env_keys) || {};
    return attr(keys.manifest || "") + "[" + count(slot) + "]";
  }

  /* ---------------- 清单：规模 KPI + 一览表 ---------------- */
  // 规模 KPI（口径 2026-09-18 用户裁决改版，2026-09-19 首卡换值）：
  //   «今日进度» = 今日已了结的账号数 ÷ 计入容量的账号数——首卡原先是「清单行数」，而行数在
  //     下面的表里一眼可见，卡片位置更该回答"今天跑得怎么样"。失败数不进这张卡：每行的「当日」
  //     列已逐执行体列了领取/完成/失败，总量再报一遍属重复；
  //   «平均每执行体分到的人数» = **计入容量的账号数** ÷ **并行执行体数**（只数「并行」行：
  //     停用与故障转移不分担账号，与后端 workers.configured 同口径），向上取整；
  //   «设定的账号容量上限» = 容量配额里的**账号**上限（与用户上限是两回事，别混用）。
  // 取不到/没配并行行显示「—」，上限 0（不限）显示「不限」——都不编造数字。
  function kpiSet(id, val, sup, emptyText) {
    var el = $(id);
    if (!el) return;
    el.textContent = "";                                // 清空容器（不用 innerHTML）
    if (val == null) {
      el.textContent = emptyText || "—";
      el.classList.add("kpi-value--empty");
      return;
    }
    el.classList.remove("kpi-value--empty");
    el.appendChild(document.createTextNode(String(val)));
    if (sup) el.appendChild(YB.el("sup", { text: sup }));
  }
  // 容量对象由页面注入（本分区读 /api/scheduler/executors，容量上限来自 /api/settings）。
  // 缺键返回 null（与 0 区分开：0 是真实值——账号上限 0 = 不限，不能当成"未知"）。
  function capacityNum(key) {
    var c = typeof ctx.capacity === "function" ? ctx.capacity() : null;
    if (!c || c[key] == null) return null;
    return count(c[key]);
  }
  // 今日进度：两个数都来自本分区的接口（activity.totals 与 current_accounts），前端只做拼接
  function progressText() {
    var counted = count(lastData && lastData.current_accounts);
    var totals = (lastData && lastData.activity && lastData.activity.totals) || {};
    return count(totals.done) + "/" + counted;
  }
  function paintKpis() {
    var loaded = !!lastData;
    var w = count(lastData && lastData.workers && lastData.workers.configured);
    var acc = capacityNum("accounts");        // 计入容量的账号数（后端唯一口径）
    var amax = capacityNum("accounts_max");   // 账号容量上限（0 = 不限）
    var per = (acc != null && w > 0) ? Math.ceil(acc / w) : null;
    kpiSet("set-exec-kpi-progress", loaded ? progressText() : null);
    kpiSet("set-exec-kpi-worker", loaded ? w : null);
    kpiSet("set-exec-kpi-perexec", loaded ? per : null, "人");
    if (!loaded) kpiSet("set-exec-kpi-capacity", null, "人", "—");
    else if (amax === 0) kpiSet("set-exec-kpi-capacity", null, "人", "不限");
    else kpiSet("set-exec-kpi-capacity", amax, "人");
  }

  // 状态与当日各占一栏（用户 2026-09-17：合在一格里两串字挤在一起，分不清哪串是状态）。
  // 徽标那层用 **inline-flex 且挂在内层 span 上**：td 直接做 flex 容器会失去
  // vertical-align:middle，内容相对同排其它列偏上（复核实测 −6.9px）
  function stateCell(row) {
    var type = attr(row.type);
    var inner = YB.el("span", { class: "set-exec-state" });
    if (type === "disabled") {
      // 停用行不报存活（后端不判它），只说明"不拉起"
      inner.appendChild(badge("停用", "badge--muted"));
      inner.appendChild(YB.el("span", { class: "set-exec-off", text: "不拉起" }));
    } else if (type === "fallback") {
      // 状态格只报状态：开关只留弹窗一个入口，同屏两个入口会让用户以为是两个独立开关。
      var fb = (lastData && lastData.fallback) || {};
      inner.appendChild(badge(FB_TEXT[fb.status] || "—", fbClass(fb)));
    } else {
      inner.appendChild(badge(STATE_TEXT[row.state] || "—", STATE_CLASS[row.state]));
    }
    return YB.el("td", {}, [inner]);
  }
  // 当日计数：按 role+index 命中该执行体；命中不到即"今日尚未运行"（后端约定的信号，显示 —）
  function dailyCell(row) {
    var type = attr(row.type);
    var a = type === "disabled" ? null
      : activityFor(type === "fallback" ? "fallback" : "worker", type === "fallback" ? null : count(row.slot));
    return YB.el("td", { class: "set-exec-daily", text: a ? dailyText(a) : "—" });
  }

  // 受门禁写操作的统一入口：先不带凭据发，由 core.js 的 dangerousSubmit 按后端 reason 补
  // 口令或倒计时确认（档位只存在于后端，本组件不判断、也不预判要不要口令）。调用方只给请求
  // 与成功回调，不再各自拼口令框管道；失败落到横幅。
  function gated(opts, onOk, failWord) {
    return withBusy(function () {
      banner("提交中…", "info");
      return YB.dangerousSubmit(opts).then(function (d) {
        return load().then(function () { return onOk(d); });
      });
    }).catch(function (e) {
      if (e && e.canceled) return canceledAfter(e);
      return failedAfter(e, failWord);
    });
  }
  // 取消弹窗与"打到一半被打回"都不是"什么都没发生"：多段保存（改出口 + 拨开关）里先成功的
  // 步骤已经写进 `.env`，若照旧静默返回或只把错误留在横幅里，用户会以为整次保存没发生、
  // 而配置已经变了。两条路径共用同一收尾——先重载视图（页面显示库里的真实状态），确有部分
  // 写入时讲明不回滚；提示语按取消/失败取词，步数由 helper 回传（见 core.js 的 dangerousSubmit）。
  function partialAfter(done, text) {
    return load().then(function () {
      // 顺序不能反：load() 走 apply() 会清掉横幅，所以提示必须落在重载之后
      setTip(text ? text + "；本次保存的前 " + done + " 步已经写入配置（部分修改已提交、"
        + "不会回滚），上面显示的是配置的当前状态。" : "", !!text);
      return false;
    });
  }
  function canceledAfter(e) {
    var done = count(e && e.completed);
    return partialAfter(done, done > 0 ? "已取消" : "");
  }
  // 一步都没提交的失败无需重载（库里没变），提示照旧就地落横幅
  function failedAfter(e, failWord) {
    var done = count(e && e.completed);
    if (!done) { failTip(e, failWord); return false; }
    return partialAfter(done, (e && e.message) || (failWord + "失败，请稍后重试"));
  }
  // 行内「更多」：停用/启用 与 删除 从行弹窗搬到这里（用户 2026-09-17：设置里不再改状态/删行，
  // 放表格操作列作为按钮，且要输主管理员密码）。复用 YB.rowMenu（portal 浮层 + 窄屏收纳）。
  function rowMenuWrap(row) {
    var type = attr(row.type);
    if (type === "fallback") return null;        // 故障转移行：类型固定、不可删除，只留「设置」
    var name = rowName(row);
    var items = [{
      label: type === "disabled" ? "启用（改回并行）" : "停用（保留出口与槽位）",
      icon: type === "disabled" ? "play" : "circle-slash",
      run: function () { changeType(row, type === "disabled" ? "worker" : "disabled"); }
    }, {
      label: "删除这一行", icon: "trash", danger: true,
      run: function () { removeRow(row); }
    }];
    var cell = YB.rowMenu.cell({ items: items, label: name + " 更多操作" });
    return cell.firstElementChild;               // 取 .dd-wrap（触发器 + 菜单）放进自己的操作格
  }

  // 改状态（停用/启用）：受门禁写操作，口令/确认由 helper 按后端 reason 收；成功/失败都走横幅
  function changeType(row, nextType) {
    var slot = count(row.slot);
    var name = rowTitle(row);
    var word = nextType === "disabled" ? "停用" : "启用";
    focusAfterPaint = { slot: slot };
    gated({
      method: "PUT", path: "/api/scheduler/executors/rows/" + slot,
      body: { type: nextType },
      desc: word + " " + name + "？请输入当前管理员密码确认。"
    }, function (d) {
      banner("已" + word + " " + name + "：" + note(d), "success");
      return true;
    }, word);
  }

  // 删行：槽位号不复用、出口配置一并删除，是破坏性动作——影响面由 confirmDialog 讲清
  // （这条说明原本挂在口令框上；口令改由 helper 按后端 reason 收，确认独立留在确认框里，
  // 故一次点击不会既弹确认框又弹口令框——两者串行，且多数档位下只有确认框）。
  function removeRow(row) {
    var slot = count(row.slot);
    var name = rowTitle(row);
    YB.confirmDialog({
      title: "删除执行体",
      body: "删除 " + name + "（槽位 " + slot + "）？它的出口配置会一并删除、槽位号不保留；"
        + "只是暂时不用请改用「停用」。",
      confirmText: "删除", danger: true
    }).then(function (ok) {
      if (!ok) return;
      focusAfterPaint = { slot: slot };
      gated({
        method: "DELETE", path: "/api/scheduler/executors/rows/" + slot, body: {},
        desc: "删除 " + name + "（槽位 " + slot + "）？请输入当前管理员密码确认。"
      }, function (d) {
        banner("已删除 " + name + "：" + note(d), "success");
        return true;
      }, "删除");
    });
  }

  // 焦点归还：保存/删除后列表整表重建，刚被 openModal 归还焦点的那颗「设置」按钮被销毁，
  // 焦点会掉到 body（复核实测）。重建后按槽位找回同一行；行已删或槽位对不上则落到「添加执行体」。
  var focusAfterPaint = null;
  function restoreFocus() {
    var want = focusAfterPaint;
    focusAfterPaint = null;
    if (!want) return;
    var el = document.querySelector('#set-exec-assign tr[data-slot="' + want.slot + '"] .set-exec-open')
      || $("set-exec-row-add");
    if (!el || el.disabled || !el.focus) return;
    var host = $("modal-host");
    // 弹窗还没真正移除时不能立刻聚焦：面板是 200ms 后从 DOM 摘掉的，摘掉那一刻浏览器会把
    // 焦点踢回 body（刚聚焦的目标也就白聚）。等 backdrop 真没了再聚焦——用 MutationObserver
    // 观察宿主，不猜动画时长（reduce 下是 0ms，同样成立）。
    if (host && host.querySelector(".pm-backdrop")) {
      var done = false;
      var obs = new MutationObserver(function () {
        if (!host.querySelector(".pm-backdrop")) go();
      });
      var go = function () {
        if (done) return;
        done = true;
        try { obs.disconnect(); } catch (e) { /* 忽略 */ }
        try { el.focus(); } catch (e) { /* 忽略 */ }
      };
      obs.observe(host, { childList: true, subtree: true });
      setTimeout(go, 800);                        // 兜底：观察器没触发也必须还焦点
      return;
    }
    try { el.focus(); } catch (e) { /* 忽略 */ }
  }

  // 故障转移开关/进程还在、但清单里已经没有那一行时点一句，免得"0 行"与卡头"正在运行"同屏无解释
  function fallbackRowMissing() {
    var fb = (lastData && lastData.fallback) || {};
    if (fb.status === "off") return "";
    var has = executors().some(function (r) { return attr(r.type) === "fallback"; });
    return has ? "" : "（清单里已没有故障转移行）";
  }

  function rowBtn(row) {
    var label = attr(row.label) || "执行体";
    var btn = YB.el("button", { type: "button", class: "btn btn--ghost btn--sm set-exec-open", text: "设置" });
    btn.setAttribute("aria-label", rowName(row) + " 设置（槽位 " + count(row.slot) + "）");
    btn.addEventListener("click", function () { openRow(row); });
    return btn;
  }

  function paintRows() {
    paintKpis();
    var tbody = $("set-exec-assign");
    if (!tbody) return;
    tbody.textContent = "";                       // 清空容器（不用 innerHTML）
    // 顺序：故障转移行**固定置顶**（用户 2026-09-17），其余按槽位号升序
    var rows = executors().slice().sort(function (a, b) {
      var af = attr(a.type) === "fallback" ? 0 : 1, bf = attr(b.type) === "fallback" ? 0 : 1;
      return af !== bf ? af - bf : count(a.slot) - count(b.slot);
    });
    rows.forEach(function (r) {
      var type = attr(r.type);
      var ops = [rowBtn(r)];
      var more = rowMenuWrap(r);
      if (more) ops.push(more);
      tbody.appendChild(YB.el("tr", {
        class: type === "disabled" ? "set-exec-row-off" : "",
        dataset: { slot: String(count(r.slot)) }
      }, [
        YB.el("td", {}, [
          YB.el("span", { class: "mono", text: rowName(r) }),
          // 停用行的标签口径就是「已停用」（后端冻结、不含槽位），多个停用行只靠标签区分不了，
          // 故这里补一个槽位号——槽位是后端给的稳定身份，不是我拼的文案。
          type === "disabled" ? YB.el("span", { class: "set-exec-slot", text: "（槽位 " + count(r.slot) + "）" }) : null
        ]),
        YB.el("td", { text: TYPE_TEXT[type] || "—" }),
        stateCell(r),
        dailyCell(r),
        YB.el("td", { text: egressText(r.egress) }),
        // flex 挂内层 span（td 做 flex 容器会失去 vertical-align:middle，≤900 换行档下按钮上浮 4.7px）
        YB.el("td", {}, [YB.el("span", { class: "set-exec-ops" }, ops)])
      ]));
    });
    // 库里的旧记录（role=unknown）：照实列出来，不猜它属于谁（后端要求：不要在前端归类）
    activities().forEach(function (a) {
      if (attr(a.role) !== "unknown") return;
      tbody.appendChild(YB.el("tr", {}, [
        YB.el("td", { text: attr(a.label) || "未标注（旧数据）" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { class: "set-exec-daily", text: dailyText(a) }),
        YB.el("td", { text: "—" }),
        YB.el("td", {})
      ]));
    });
    if (!rows.length && !activities().length) {
      tbody.appendChild(YB.el("tr", {}, [
        YB.el("td", { text: "清单为空：点表底的「添加执行体」加一行" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "—" }),
        YB.el("td", { text: "—" }),
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
    setText("set-exec-fb-text", (fb.status === "declared_not_running"
      ? "已声明开启但没有进程在跑。"
      : (fb.status === "running_not_declared" ? "有进程在跑，但不是由配置拉起的。" : ""))
      + fallbackRowMissing());
    setHidden($("set-exec-fb-state"), !fbAbnormal(fb));
    // 报警纪律（后端要求）：只有"开了却没跑起来"**且落在本应运行时段内**才报警
    var alarm = fb.status === "declared_not_running" && fb.in_window === true;
    var warn = $("set-exec-fb-warn");
    if (warn) {
      warn.textContent = alarm
        ? "故障转移行声明已开启但当前没有在跑（且正在签到时段内）：窗口内的漏签不会被补，"
          + "请检查宿主 cron（或容器调度器）是否以 --fallback 拉起。"
        : "";
      warn.hidden = !alarm;
    }
  }

  /* ---------------- 写入：行增删 / 改类型 / 改出口（都立即落盘） ---------------- */
  function note(d) { return attr(d && d.note) || "已写入配置"; }
  function failTip(e, what) { setTip((e && e.message) || (what + "失败，请稍后重试"), true); }

  // 在途终态：按钮跟着 busy 灰掉，"点了没反应"变成"按钮灰着"
  function setBusy(on) {
    var b = $("set-exec-row-add");
    if (b) b.disabled = !!on || !ctx.isMaster;
  }

  // 失败时**不吞错误、也不还焦点**：本分区写动作经 gated 走受门禁提交，helper 收口令/倒计时
  // 框期间焦点与错误都该留在框内（拒绝时它自己把后端文案显示在框里、允许改口令重试），
  // 不能被 restoreFocus 抢走；成功分支才归还焦点。
  function withBusy(fn) {
    if (busy) return Promise.resolve(false);
    busy = true;
    setBusy(true);
    function cleanup() { busy = false; applyPerm(); setBusy(false); }
    return Promise.resolve().then(fn).then(function (ok) {
      cleanup();
      if (focusAfterPaint) restoreFocus();       // 必须在 busy 复位后：禁用按钮 focus() 无效
      return ok;
    }, function (e) {
      cleanup();
      focusAfterPaint = null;
      throw e;
    });
  }

  // 追加行：受门禁写操作（追加一定改配置），口令/确认由 helper 按后端 reason 收
  function addRow() {
    if (!ctx.isMaster || busy) return;          // busy 是防重入的唯一判据，入口再挡一道
    gated({
      method: "POST", path: "/api/scheduler/executors/rows", body: { type: "worker" },
      desc: "添加一行执行体（并行、默认直连）？追加行会改动执行体清单，请输入当前管理员密码确认。"
    }, function (d) {
      setTip("已添加「并行执行体 #" + (count(d && d.slot) + 1) + "」（默认直连）：" + note(d)
        + "。想给它单独出口，点那一行的「设置」；编号只增不复用，故障转移行也占一个编号"
        + "（它固定置顶、行名不带数字），所以并行行跳号是正常的、不影响运行"
        + "（没删过行却看到跳号，就是它在占号）。", false);
      focusAfterPaint = { slot: count(d && d.slot) };   // 焦点落到新行的「设置」（busy 复位后归还）
      return true;
    }, "添加");
  }

  /* ---------------- 行内弹窗：类型 + 出口（低频与破坏性动作降到正文里） ---------------- */
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
  var ROW_HELP = "名称：只影响本页显示（留空＝用默认名）；故障转移行的名称固定。"
    + "类型：并行＝会被拉起；故障转移＝窗口内补签，最多一行、固定置顶；"
    + "停用＝保留出口与槽位、不拉起。改类型与删除在表格「操作」列的更多菜单里，且要输一次管理员密码。"
    + "出口：读接口只回脱敏描述串（不含账号密码），本框不回显原值，留空＝不修改；"
    + "改成直连请用下面的「清除出口」。写入只动这一行，其余行（含停用行的出口）逐字保留；"
    + "改动即时写入配置，下一轮定时任务或重启执行体/容器后生效。";

  function linkBtn(text, onClick, extraClass) {
    var b = YB.el("button", { type: "button", class: "linklike " + (extraClass || ""), text: text });
    b.addEventListener("click", onClick);
    return b;
  }

  // 单并行行的出口提示：清单只有 1 个「并行」行时运行时走进程内路径（`single` 角色），
  // 出口读全局配置键（`workers.env_keys.single`，即 YIBAN_PROXY），本行这一格要到出现第二个
  // 并行行后才按行生效。不提示的话用户会以为行内出口已生效——而页面此时显示的正是行内值。
  // 判据用后端 `workers.configured`（只数 worker 行，0 行时按契约仍为 1 = 单执行体形态），
  // 键名同样由接口下发，本文件不写死配置键名。
  function singleRowHint(type) {
    if (type !== "worker") return null;
    if (count(lastData && lastData.workers && lastData.workers.configured) > 1) return null;
    var key = attr(((lastData && lastData.workers && lastData.workers.env_keys) || {}).single);
    var box = YB.el("p", { class: "alert info set-exec-hint", role: "status" });
    var ico = YB.el("span", { class: "ico" });
    ico.appendChild(YB.iconEl("info"));
    box.appendChild(ico);
    box.appendChild(YB.el("span", { class: "body", text: "只有一个并行执行体时，程序在本进程内直接签到，"
      + "出口读全局配置「" + key + "」（不是本行这一格）；加到第二个并行执行体后才按行生效。" }));
    return box;
  }

  function openRow(row) {
    if (!lastData) { setTip("数据尚未加载完成", true); return null; }
    var slot = count(row.slot);
    var type = attr(row.type);
    var isFb = type === "fallback";
    var fb = (lastData.fallback) || {};

    // .set-exec-form：给弹窗内相邻字段之间补垂直间距（见 app.css；.field 自身没有外边距）
    var wrap = YB.el("div", { class: "set-exec-form" });
    var hint = singleRowHint(type);
    if (hint) wrap.appendChild(hint);
    // 开关放首位：它是这一行的主状态控件，紧随其后的类型/出口字段才聚成一组（接近性原则）。
    var swInput = null;
    if (isFb) {
      swInput = YB.el("input", { type: "checkbox" });
      if (fb.enabled === true) swInput.checked = true;
      var swHelpId = "set-exec-modal-fb-help";
      swInput.setAttribute("aria-describedby", swHelpId);
      wrap.appendChild(YB.el("div", { class: "field" }, [
        YB.el("span", { class: "field-label", text: "故障转移开关" }),
        YB.el("label", { class: "switch", title: "开启故障转移" }, [
          swInput, YB.el("span", { class: "track", "aria-hidden": "true" }),
          YB.el("span", { class: "sr-only", text: "开启故障转移" })
        ]),
        YB.el("p", { class: "field-help", id: swHelpId, text: "只写声明开关：窗口内补签还需部署侧以 --fallback 拉起进程才会真在跑。" })
      ]));
    }
    // 名称：后端 2026-09-17 起每行都下发 name（未设 = null），故能力探测恒真——保留探测只是为了
    // 字段将来消失时不会做出"点了会 400"的输入框。`label` 是后端口径、`name` 是用户输入，
    // 只拿 name 回填输入框（placeholder 用 label 提示默认名）。故障转移行的名称由后端定，不给改名。
    var nameId = "set-exec-modal-name";
    if (!isFb && Object.prototype.hasOwnProperty.call(row, "name")) {
      wrap.appendChild(YB.el("div", { class: "field" }, [
        YB.el("label", { class: "field-label", for: nameId, text: "名称（留空 = 用默认名）" }),
        YB.el("div", { class: "input-group" }, [
          YB.el("input", {
            // maxlength 与后端上限一致（32）；换行在单行 input 里本来就打不进来，
            // 粘贴带换行会被浏览器剥掉——故不另加前端校验，直接以后端 400 文案兜底。
            class: "input", id: nameId, type: "text", autocomplete: "off", maxlength: "32",
            placeholder: attr(row.label) || "并行执行体 #" + (slot + 1), value: attr(row.name)
          })
        ]),
        YB.el("p", { class: "field-help", text: "只影响本页显示（最长 32 个字符）。" })
      ]));
    }
    // 类型：只读展示（用户 2026-09-17：设置里不再改类型，停用/启用移到操作列）；
    // 改法（怎么改类型、怎么删行）在旁边的 ⓘ 里，不在页面上重复一遍。
    wrap.appendChild(YB.el("div", { class: "field" }, [
      YB.el("span", { class: "field-label" }, [
        YB.el("span", { text: "类型" }),
        infoTip("类型与出口的改法", ROW_HELP)
      ]),
      YB.el("p", { class: "set-summary", text: TYPE_TEXT[type] + (isFb ? "（固定：置顶、不可改类型、不可删除）" : "") })
    ]));

    // 「当前出口」与「对应配置项」合并成一行（用户 2026-09-17：这两条本来在说同一件事）
    wrap.appendChild(YB.el("div", { class: "field" }, [
      YB.el("span", { class: "field-label", text: "当前出口（已脱敏）" }),
      YB.el("p", { class: "set-summary", text: egressText(row.egress) + "｜配置项 " + manifestKey(slot) })
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

    // 存活：只报表格那两列看不到的事实——最近活跃时刻、故障转移是否在当前应运行时段内。
    // （状态与当日已在表格里各占一栏，此处不重复。）
    if (type !== "disabled") {
      wrap.appendChild(YB.el("p", { class: "set-summary", text: isFb
        ? "存活：" + (FB_TEXT[fb.status] || "—") + (fb.in_window ? "（当前在本应运行时段内）" : "（当前不在本应运行时段内）")
        : "存活：" + (STATE_TEXT[row.state] || "—") + (row.last_seen_at ? "；最近活跃 " + attr(row.last_seen_at) : "") }));
    }

    // 低频且破坏性的动作降级到正文里（用户 2026-09-17：「清除出口」与「保存」不是一个视觉层级）
    wrap.appendChild(YB.el("p", { class: "set-exec-subactions" }, [
      linkBtn("清除出口（改为直连）", function () {
        if (handle && handle.close) handle.close();
        focusAfterPaint = { slot: slot };
        // 清除出口 = 改 proxy，是受门禁写操作；原出口配置会被删掉且不可撤销，
        // 故影响面由 confirmDialog 讲清，口令/确认交给 helper 按后端 reason 收
        YB.confirmDialog({
          title: "清除出口",
          body: "清除 " + rowTitle(row) + " 的出口（改为直连）？原出口配置会从配置项里删掉，不可撤销。",
          confirmText: "清除", danger: true
        }).then(function (ok) {
          if (!ok) return;
          gated({
            method: "PUT", path: "/api/scheduler/executors/rows/" + slot, body: { proxy: "" },
            desc: "清除 " + rowTitle(row) + " 的出口（改为直连）？请输入当前管理员密码确认。"
          }, function (d) {
            setTip("已清除出口：" + note(d), false);
            return true;
          }, "清除");
        });
      })
    ]));
    function switchArg() {
      if (!isFb || !swInput) return null;
      return swInput.checked === (fb.enabled === true) ? null : (swInput.checked ? 1 : 0);
    }

    // 保存：**改出口或开关进门禁，只改名不进**（`PUT …/rows` 在 type/proxy 真的会变时才判，
    // `PUT …/executors` 同理）——名字不影响行为，按后端口径不打这道门，故只改名时直接发。
    // 门禁请求先不带凭据发，由 helper 按后端 reason 收口令；两个端点一次点击最多问一次口令。
    function save() {
      if (busy) return false;
      var egress = (($(inputId) || {}).value || "").trim();   // 留空＝不改出口（空串语义是"直连"，故不发）
      var nameEl = $(nameId);
      var newName = nameEl ? nameEl.value.trim() : null;
      var nameArg = (nameEl && newName !== attr(row.name)) ? newName : null;   // null = 没改名
      var enableArg = switchArg();                                            // null = 开关没动
      if (!egress && nameArg == null && enableArg == null) {
        banner("没有需要保存的改动（留空 = 不修改出口；改成直连请点上面的「清除出口」）。", "info");
        return false;
      }
      // 取值必须**在这里取完**再关弹窗：关掉后输入框被摘出 DOM，再按 id 取就是 null
      // （实测踩过：请求只带了凭据、没带要改的字段，后端回 400「没有可更新的字段」）。
      var rowBody = {};
      if (egress) rowBody.proxy = egress;
      if (nameArg != null) rowBody.name = nameArg;
      var requests = [];
      // 行接口缺 type/proxy/name 会回 400「没有可更新的字段」；只拨开关时不能发这个空体
      if (egress || nameArg != null) {
        requests.push({ method: "PUT", path: "/api/scheduler/executors/rows/" + slot, body: rowBody });
      }
      var needsPw = !!egress || enableArg != null;
      if (enableArg != null) {
        requests.push({ method: "PUT", path: "/api/scheduler/executors", body: { fallback_enable: enableArg } });
      }
      if (handle && handle.close) handle.close();
      focusAfterPaint = { slot: slot };
      if (!needsPw) { direct(requests[0]); return true; }
      // 措辞随本次实际要写的项取词：只拨开关时不能说成"改出口"
      var desc = (egress
        ? "修改 " + rowTitle(row) + " 的出口配置" + (enableArg != null ? "与故障转移开关" : "")
        : (enableArg ? "开启" : "关闭") + "故障转移") + "？请输入当前管理员密码确认。";
      gated({ requests: requests, desc: desc }, function (res) {
        banner("已保存 " + rowTitle(row) + "：" + note(res && res[0]), "success");
        if (focusAfterPaint) restoreFocus();
        return true;
      }, "保存");
      return true;
    }
    // 只改名：不进门的直接提交，成功/失败都走横幅
    function direct(req) {
      if (busy) return false;
      return withBusy(function () {
        banner("提交中…", "info");
        return YB.api(req.method, req.path, req.body).then(function (d) {
          return load().then(function () {
            banner("已保存 " + rowTitle(row) + "：" + note(d), "success");
            if (focusAfterPaint) restoreFocus();
            return true;
          });
        });
      }).catch(function (e) { failTip(e, "保存"); return false; });
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
    return handle;
  }

  /* ---------------- 容量建议与耗时实测：已移到「容量配额」分区（2026-09-17） ----------------
     这两件事与容量配额共用同一份容量数据（计入容量的账号数、有效窗口），用户要求同类功能
     同屏，故连代码一起搬到 components/settings-quota.js（applyExecutors/measure），
     本分区只把接口响应通过 onData 交给它。实测那两个风险（默认拿列表第一个账号、只覆盖
     窗口外最小链路偏乐观）改挂在容量配额页那张卡的 ⓘ 里。 */

  function apply(data) {
    lastData = data || null;
    paintKpis();
    paintRows();
    paintFallback(data);
    setTip("", false);
    applyPerm();
    // 同一次响应里还有容量建议（measured / recommendation / window / current_accounts），
    // 归「容量配额」分区的卡片渲染——一份数据一次请求，两个分区各取所需。
    if (typeof ctx.onData === "function") ctx.onData(data);
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

  // options: { isMaster, capacity: () => (容量对象), onData: (payload) => void }
  //   capacity：规模 KPI 要「计入容量的账号数」与「账号容量上限」（`accounts` / `accounts_max`），
  //             它来自 /api/settings（页面持有）——用回调取值，避免本组件再请求一次；
  //             上限改了由页面调 refreshKpis() 重画。
  //   onData  ：把同一份响应交给「容量配额」分区的卡片（容量建议与实测）。
  function mount(options) {
    options = options || {};
    ctx = {
      isMaster: !!options.isMaster,
      capacity: typeof options.capacity === "function" ? options.capacity : null,
      onData: typeof options.onData === "function" ? options.onData : null
    };
    var addBtn = $("set-exec-row-add");
    if (addBtn) addBtn.addEventListener("click", addRow);
    applyPerm();
  }

  YB.settingsExecutors = {
    mount: mount,
    load: load,
    apply: apply,
    refreshKpis: paintKpis,
    // 清单模型下每个写操作**立即落盘**，没有"待保存的整页改动"——保留下面两个方法只为满足
    // 页面统一的「未保存改动守卫」接口（见 pages/work_settings.js 的 stores()）。
    save: function () { return Promise.resolve(true); },
    isDirty: function () { return false; }
  };
})();
