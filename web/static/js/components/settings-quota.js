/* 系统设置 · 容量配额分区（管理端 /work/settings）。

   挂载到 window.YB.settingsQuota；classic script。仅主管理员可改（档位单源是后端
   `web/app.py` 的 `MASTER_ONLY_KEYS`，本文件不再抄第二份键名清单；字段名保持
   `body.<键> = …` 直写形态供对拍测试读取）。
   非主管理员：控件禁用 + 就地说明（可见即理解权限）。

   两张卡：
     1) 容量配额（本文件的主职）：只保留上限编辑——只读用量三分类与容量估算已删除，
        /accounts 的容量卡已给出同口径展示，设置页不再重复；
     2) 容量建议与耗时实测（2026-09-17 从「执行体」分区搬来）：建议值与实测共用容量数据
        （计入容量的账号数、有效窗口、实测容量），同类功能同屏；数据经 applyExecutors()
        由页面转交，本文件不重复请求。实测只判主管理员 + 后端全局冷却（429 + 倒计时），
        **不带口令框**——后端 2026-09-17 判它与手动签到同档（不改配置），前端不放假门；
        页面只保留一道诚实的二次确认。

   保存语义（与全页统一）：改动只标脏（脏徽标 + 保存按钮出现），点「保存容量上限」
   才提交；保存是受门禁操作，走统一 helper——先不带凭据发，后端回 reason 才补口令
   （不合适的上限会影响新增注册/账号），成功后回调页面刷新容量状态。
   对外面：mount(options) / apply(data) / applyExecutors(data) / save() → Promise<boolean>。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ctx = { isMaster: false, onSaved: null };
  var snap = { users: 0, accounts: 0 };
  var dirty = false;
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  function setText(id, text) {
    var n = $(id);
    if (n) n.textContent = text == null ? "" : String(text);
  }
  function setTip(text, bad) { YB.setTip("set-cap-tip", text, bad); }
  function value(id, fallback) {
    var n = parseInt(($(id) || {}).value, 10);
    return isNaN(n) ? fallback : n;
  }
  function markDirty() {
    if (dirty) return;
    dirty = true;
    setHidden($("set-cap-save"), false);
    setHidden($("set-cap-dirty"), false);
  }
  function clearDirty() {
    dirty = false;
    setHidden($("set-cap-save"), true);
    setHidden($("set-cap-dirty"), true);
  }

  // 容量上限按权限启用/禁用；禁用时把原因 #set-cap-perm 与控件做程序化关联（读屏可及）。
  function applyPerm() {
    var disabled = !ctx.isMaster;
    ["set-max-users", "set-max-accounts"].forEach(function (id) {
      var n = $(id);
      if (!n) return;
      n.disabled = !!disabled;
      if (disabled) n.setAttribute("aria-describedby", "set-cap-perm");
      else n.removeAttribute("aria-describedby");
    });
    var btn = $("set-cap-save");
    if (btn) btn.disabled = !!disabled;
    setHidden(btn, disabled || !dirty);
    setHidden($("set-cap-dirty"), disabled || !dirty);
    setHidden($("set-cap-perm"), !disabled);
  }

  function collect() {
    var body = {};
    var u = value("set-max-users", snap.users);
    var a = value("set-max-accounts", snap.accounts);
    if (u !== snap.users) body.max_users = u;
    if (a !== snap.accounts) body.max_accounts = a;
    return body;
  }

  // 受门禁的保存：**先不带凭据发**，由后端 reason 决定要不要口令（档位只存在于后端）；
  // 用户取消弹窗 = 本次不保存。
  function submit(body) {
    busy = true;
    setTip("保存中…", false);
    return YB.dangerousSubmit({
      method: "POST", path: "/api/settings", body: body,
      desc: "调整容量上限：不合适的设置可能影响新增注册/账号，是否继续？\n请输入当前管理员密码确认。"
    }).then(function (data) {
      snap = { users: value("set-max-users", snap.users), accounts: value("set-max-accounts", snap.accounts) };
      clearDirty();
      setTip((data && data.msg) || "容量上限已保存", false);
      if (ctx.onSaved) ctx.onSaved(data);
      return true;
    }, function (e) {
      if (e && e.canceled) setTip("", false);
      else setTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      busy = false;
      applyPerm();
      return ok;
    });
  }

  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 取消或失败。
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

  function apply(data) {
    var cap = (data && data.capacity) || {};
    snap = { users: Number(cap.users_max) || 0, accounts: Number(cap.accounts_max) || 0 };
    var u = $("set-max-users"), a = $("set-max-accounts");
    if (u) u.value = String(snap.users);
    if (a) a.value = String(snap.accounts);
    clearDirty();
    setTip("", false);
    applyPerm();
  }

  /* ================= 容量建议与耗时实测 =================
     从「执行体」分区搬来（用户 2026-09-17）：两件事共用同一份容量数据（计入容量的账号数、
     有效窗口、实测容量），同类功能同屏对照，且**直接放页面上**、不再收进弹窗。
     数据来源仍是 GET /api/scheduler/executors（由页面在 settingsExecutors.load() 之后
     经 onData 转交，本文件不重复请求）；measured / recommendation 都由后端算好，前端不换算。
     契约要求：measured 为 null 时必须显示「未实测」且不给建议，不得编造数字。 */
  var execData = null;
  var measuring = false;
  var coolTimer = null;
  var coolUntil = 0;          // 冷却截止时刻（毫秒）；后端是全局冷却，这里只用来禁用按钮与显示倒计时
  var lastResult = "";        // 上一次实测结果（或失败原因）：冷却倒计时追加在它后面，不覆盖
  var measureBad = false;     // 上面那句是失败态 → 结果行上红

  function count(v) { return Number(v) || 0; }
  function attr(v) { return v == null ? "" : String(v); }
  function measureOut(text, bad) {
    var out = $("set-cap-measure-result");
    if (!out) return;
    out.textContent = text == null ? "" : String(text);
    out.className = bad ? "set-tip set-bad" : "set-tip";
  }
  function clockText(sec) {
    if (sec < 60) return Math.max(1, Math.round(sec)) + " 秒";
    var m = Math.floor(sec / 60), s = Math.round(sec % 60);
    return m + " 分" + (s ? " " + s + " 秒" : "");
  }
  function coolLeft() {
    var left = (coolUntil - Date.now()) / 1000;
    return left > 0 ? left : 0;
  }
  // 建议只有一行摘要；没有实测值就明说"未实测"并指向写入处（配置文件，页面上无录入控件），
  // 不是让用户点按钮硬凑数字
  function adviceLine(d) {
    var win = (d && d.window) || {};
    var rec = (d && d.recommendation) || null;
    var measured = (d && d.measured) || null;
    if (!measured) {
      return "未实测：配置文件里还没有实测容量，因此不给出建议值；可先点右上角「测试单账号耗时」粗量一次，再由部署者写入配置文件。";
    }
    return "建议并行执行体数 " + (rec ? count(rec.executors_needed) + " 个" : "—")
      + "（每个约 " + (rec ? count(rec.per_executor_accounts) : "—") + " 个账号，含慢账号余量）"
      + " · 实测容量 " + count(measured.per_executor_capacity) + " 个"
      + " · 计入容量 " + count(d && d.current_accounts) + " 个账号"
      + (win.start && win.end ? " · 有效窗口 " + win.start + " ~ " + win.end : "");
  }
  function paintAdvice() {
    setText("set-cap-advice-line", execData ? adviceLine(execData) : "尚未读取执行体数据");
    var rec = (execData && execData.recommendation) || null;
    var note = attr(rec && rec.note);
    var noteEl = $("set-cap-advice-note");
    if (noteEl) { noteEl.textContent = note; noteEl.hidden = !note; }
  }

  // 冷却/在途：按钮灰掉 + 剩余时间**追加**在结果行末尾（"点了没反应"变成"还要等多久"）。
  // 追加而不是替换：实测结果本身是要看的内容，倒计时只是补充说明（早先替换过一次，
  // 结果刚测出来就被倒计时顶掉、10 分钟内看不到数字——实机复现）。
  // 后端已按全局冷却拦（429 带 next_allowed_in），前端这份只是把状态显示出来。
  function paintButton() {
    var btn = $("set-cap-measure");
    if (!btn) return 0;
    var left = coolLeft();
    btn.disabled = measuring || left > 0 || !ctx.isMaster;
    if (left > 0) btn.title = "冷却中，还需 " + clockText(left);
    else btn.removeAttribute("title");
    return left;
  }
  function tick() {
    var left = paintButton();
    if (!left && coolTimer) { clearInterval(coolTimer); coolTimer = null; }
    measureOut(lastResult + (left ? (lastResult ? "　" : "") + "冷却中：还需 " + clockText(left)
      + " 才能再测（后端全局冷却，连点只会产生一次真实登录）。" : ""), measureBad);
  }
  // 实测（POST /measure）：真的会用真实账号访问一次易班（只读、不签到）。
  // **口令门**：后端 2026-09-17 的口径是"不改配置 → 不要求 confirm_password"（与手动签到同档，
  // 只判主管理员 + 冷却），故这里**不放口令框**——前端弹一个后端不校验的口令框就是"假门"
  // （docs/refactor/88 §2.5 与安全复审 P1 都说清过；该目录未纳入版本控制，接口契约另见
  // docs/dev/api-executors.md 的口令门一节）。要改成口令门需后端加一行校验，前端再跟上。
  // 这里保留一道**诚实的二次确认**：一次实测会拿真实账号真登录一次，值得让操作者按一下。
  // pwOpen：确认框已在途时不再叠开第二个。
  var pwOpen = false;
  function measure() {
    if (measuring || pwOpen || !ctx.isMaster || coolLeft() > 0) return;
    pwOpen = true;
    YB.confirmDialog({
      title: "现场实测单账号耗时？",
      body: "会用一位真实账号登录一次易班（只读、不签到，不写签到状态、不占领取池），"
        + "并占用全局实测冷却。确认后立即开始。",
      confirmText: "开始实测"
    }).then(function (ok) {
      pwOpen = false;
      if (ok) run();
    });
  }
  function run() {
    if (measuring) return Promise.resolve(false);   // 在途：不重复发（按钮此时也是禁用的）
    measuring = true;
    measureBad = false;
    paintButton();
    measureOut("实测中…（会用一位真实账号登录一次易班，只读、不签到）", false);
    return YB.api("POST", "/api/scheduler/executors/measure", {}).then(function (d) {
      var sec = d && d.seconds != null ? Number(d.seconds) : null;
      var cap = count(d && d.per_executor_capacity);
      var per = count(d && d.recommended_per_executor);
      var recd = execData && execData.measured;
      lastResult = "实测 " + (sec == null ? "—" : sec.toFixed(2) + " 秒")
        + "（样本 " + attr(d && d.sample) + "）：单执行体容量约 " + cap + " 个、建议每执行体 " + per + " 个账号。"
        + "这是窗口外粗量（只覆盖登录 + 拉任务，比真实签到偏乐观）且不会自动保存"
        + (recd ? "；当前已录入的实测容量是 " + count(recd.per_executor_capacity) + " 个" : "")
        + "。";
      measureBad = false;
      var cd = count(d && d.cooldown_sec);
      if (cd > 0) coolUntil = Date.now() + cd * 1000;
      var out = $("set-cap-measure-result");
      if (out && d && d.note) out.title = attr(d.note);   // 后端原口径挂在 title 上备查
      return true;
    }, function (e) {
      var left = e && e.data && Number(e.data.next_allowed_in);
      if (left > 0) coolUntil = Date.now() + left * 1000;   // 429：冷却还剩多久由后端给
      lastResult = (e && e.message) || "实测失败，请稍后重试";
      measureBad = true;
      return false;
    }).then(function (ok) {
      measuring = false;
      if (coolLeft() > 0 && !coolTimer) coolTimer = setInterval(tick, 1000);
      tick();                                                 // 立刻按"结果 + 冷却"渲染一次
      return ok;
    });
  }

  // 页面在每次 GET /api/scheduler/executors 之后调这里（非主管理员不会被调到）
  function applyExecutors(data) {
    execData = data || null;
    paintAdvice();
    paintButton();
    // 后端上一次的冷却窗口对页面是未知的：只能等首次 429 才知道，故这里不猜、不预置倒计时
  }

  function mount(options) {
    ctx = {
      isMaster: !!(options && options.isMaster),
      onSaved: options && typeof options.onSaved === "function" ? options.onSaved : null
    };
    var btn = $("set-cap-save");
    if (btn) btn.addEventListener("click", function () { save(); });
    ["set-max-users", "set-max-accounts"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", function () {
        var cur = { users: value("set-max-users", snap.users), accounts: value("set-max-accounts", snap.accounts) };
        if (cur.users === snap.users && cur.accounts === snap.accounts) clearDirty();
        else markDirty();
      });
    });
    // 建议与实测卡：整块仅主管理员可见（那个接口对其它身份 403，显示空卡比不显示更糟）
    setHidden($("set-cap-advice"), !ctx.isMaster);
    var m = $("set-cap-measure");
    if (m) m.addEventListener("click", measure);
    applyPerm();
  }

  YB.settingsQuota = {
    mount: mount,
    apply: apply,
    applyExecutors: applyExecutors,
    save: save,
    isDirty: function () { return dirty; }
  };
})();
