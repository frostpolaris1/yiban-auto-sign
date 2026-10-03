/* 系统设置页（管理端 /work/settings）口径层——纯函数，无 DOM / 无网络 / 无全局。
   刻意用纯 JS（不是 TS）：多处 Python 守卫会把这里的具体函数按花括号配对抽出、放进
   node 真跑（`edgeMaxMin` 与服务端 `edge_cap_sec` 逐值对拍、`norm` 的范围回退、
   `resolvePaintValue` 的"未知枚举不静默换值"），TS 的类型注解 / import 会让抽取失败。
   改动本文件前先读 tests/test_schedule_edge_limit_js.py、test_time_field_norm.py、
   test_env_line_break_frontend.py。

   与 legacy 的关系（整页迁移 P3）：
   · legacy `components/settings-{schedule,notify,mail,quota,health,switches,dist-viz}.js`
     的**纯计算**部分全部收进这里；DOM 渲染与写操作分别由各 .vue 组件与 `ops.js` 承担。
   · 字段名保持 `body.<键> = …` 直写形态，供 tests/test_settings_tiers_frontend_parity.py
     的档位对拍静态读取（键必须是后端档位表里的键）。

   正态 μ/σ 的数学（pdf / 包络 / σ_eff / 拖拽映射）也在此：settings-dist-viz 的 canvas
   手势依赖它们，抽成纯函数后可用 Vitest 直测（原 613 行组件零测试）。 */

/* =========================================================================
   签到调度
   ========================================================================= */

/* 与后端 `yiban.engine.schedule._DEFAULT_*` 同值：仅用于空输入/缺字段的兜底回显。 */
export var SCHEDULE_DEFAULTS = {
  order: "sequence", dist: "uniform", edge: 60, start: "06:30", end: "07:50",
  muMin: 40, muMax: 60, sigmaMin: 15, sigmaMax: 25,
};

/* 掐头/去尾：0.5 分钟对齐后转秒（0~5 分钟）。 */
export function edgeVal(v) {
  var n = parseFloat(v);
  if (isNaN(n)) return 0;
  n = Math.min(5, Math.max(0, n));
  return Math.round(n * 2) / 2 * 60;
}

/* 缓冲单边上限（分钟）：与 `yiban.window.edge_cap_sec` 同一条式子（窗口宽度的 20%、
   封顶 5 分钟、按 30s 粒度向下取整）。窗口不可用（宽度 <= 0）时上限为 0，**不退回最大
   量程**——否则窗口倒置（起 >= 止）会把缓冲上限放到最大（fail-open），而服务端夹取
   只会给出 0，前端宽服务端窄正是"保存后数字变小"的来源。 */
export function edgeMaxMin(winSec) {
  var cap = Math.floor(winSec * 0.2 / 30) * 30 / 60;
  return Math.max(0, Math.min(5, cap));
}

/* 账号间隔上界 3600 与后端钳位一致：避免前端按未钳位值提示"已保存"而后端静默改小。 */
export function clampGap(v) {
  v = parseInt(v, 10);
  if (isNaN(v)) return 0;
  return Math.min(3600, Math.max(0, v));
}

/* μ/σ 百分比：整数 0~100（与服务端校验同一范围）。 */
export function pctVal(v, fallback) {
  var n = parseInt(v, 10);
  if (isNaN(n)) return fallback;
  return Math.min(100, Math.max(0, n));
}

/* "HH:MM" → 当天分钟数；形状或范围不符返回 null。 */
export function hhmmToMin(v) {
  var m = /^(\d{2}):(\d{2})$/.exec(String(v == null ? "" : v).trim());
  if (!m) return null;
  var h = parseInt(m[1], 10), mi = parseInt(m[2], 10);
  if (h > 23 || mi > 59) return null;
  return h * 60 + mi;
}

/* 窗口宽度（秒）：结束晚于开始才为正，否则 0（与服务端 fail-closed 同口径）。 */
export function windowSec(start, end) {
  var s = hhmmToMin(start), e = hhmmToMin(end);
  if (s == null || e == null) return 0;
  return e > s ? (e - s) * 60 : 0;
}

/* 时间字段 `norm`：形状 **与** 00:00–23:59 同一处校验。
   只查形状会让 "25:00" 当作合法值一路通过——触发器文案、隐藏值、服务端生效值三段不等。 */
export function norm(v, fallback) {
  var s = String(v == null ? "" : v).trim().slice(0, 5);
  var m = /^(\d{2}):(\d{2})$/.exec(s);
  if (!m || parseInt(m[1], 10) > 23 || parseInt(m[2], 10) > 59) return fallback;
  return s;
}

/* 下拉"值 → 可见态"的纯解析：命中返回对应项，未命中**原样返回该值**（渲染为未知态）。
   刻意不做"未知值退回首项"：那会静默把服务器上的未知枚举换成首项，于是下一次无关保存
   把首项当成"用户改动"写进 .env（配置被静默改写）。 */
export function resolvePaintValue(opts, v) {
  var i;
  for (i = 0; i < opts.length; i++) {
    if (opts[i].v === v) return { value: v, hit: opts[i] };
  }
  return { value: v, hit: null };
}

/* 调度快照（用于只提交改动字段）：从服务端响应构造。 */
export function scheduleSnapshot(data) {
  data = data || {};
  var d = SCHEDULE_DEFAULTS;
  return {
    order: data.sign_order || d.order,
    dist: data.sign_dist || d.dist,
    edgeFront: Number(data.edge_front_sec != null ? data.edge_front_sec
      : (data.window_edge_sec != null ? data.window_edge_sec : d.edge)),
    edgeBack: Number(data.edge_back_sec != null ? data.edge_back_sec
      : (data.window_edge_sec != null ? data.window_edge_sec : d.edge)),
    gap: Number(data.gap_max != null ? data.gap_max : 0),
    pref: data.allow_time_pref ? 1 : 0,
    sat: data.saturday_sign ? 1 : 0,
    sun: data.sunday_sign ? 1 : 0,
    window: data.sign_window || (d.start + " ~ " + d.end),
    muMin: Number(data.mu_min_pct != null ? data.mu_min_pct : d.muMin),
    muMax: Number(data.mu_max_pct != null ? data.mu_max_pct : d.muMax),
    sigmaMin: Number(data.sigma_min_pct != null ? data.sigma_min_pct : d.sigmaMin),
    sigmaMax: Number(data.sigma_max_pct != null ? data.sigma_max_pct : d.sigmaMax),
  };
}

/* 从当前表单值构造调度快照（保存成功后回写 snap；与 collect 同口径）。 */
export function scheduleFormSnapshot(form) {
  return {
    order: form.order || SCHEDULE_DEFAULTS.order,
    dist: form.dist || SCHEDULE_DEFAULTS.dist,
    edgeFront: edgeVal(form.edgeFront),
    edgeBack: edgeVal(form.edgeBack),
    gap: clampGap(form.gap),
    pref: form.pref ? 1 : 0,
    sat: form.sat ? 1 : 0,
    sun: form.sun ? 1 : 0,
    window: form.windowStart + " ~ " + form.windowEnd,
    muMin: pctVal(form.muMin, SCHEDULE_DEFAULTS.muMin),
    muMax: pctVal(form.muMax, SCHEDULE_DEFAULTS.muMax),
    sigmaMin: pctVal(form.sigmaMin, SCHEDULE_DEFAULTS.sigmaMin),
    sigmaMax: pctVal(form.sigmaMax, SCHEDULE_DEFAULTS.sigmaMax),
  };
}

/* 只提交相对快照真正变化的字段（部分更新；非主管理员因此只送得出可改字段）。 */
export function scheduleBody(snap, form) {
  var body = {};
  var now = scheduleFormSnapshot(form);
  if (now.order !== snap.order) body.sign_order = now.order;
  if (now.dist !== snap.dist) body.sign_dist = now.dist;
  if (now.edgeFront !== snap.edgeFront) body.edge_front_sec = now.edgeFront;
  if (now.edgeBack !== snap.edgeBack) body.edge_back_sec = now.edgeBack;
  if (now.pref !== snap.pref) body.allow_time_pref = now.pref;
  if (now.sat !== snap.sat) body.saturday_sign = now.sat;
  if (now.sun !== snap.sun) body.sunday_sign = now.sun;
  if (now.window !== snap.window) body.sign_window = now.window;
  if (now.gap !== snap.gap) body.gap_max = now.gap;
  // μ/σ 区间：lo>=hi 也照发（服务端"告警 + 回退默认"语义），只是就地预警
  if (now.muMin !== snap.muMin) body.mu_min_pct = now.muMin;
  if (now.muMax !== snap.muMax) body.mu_max_pct = now.muMax;
  if (now.sigmaMin !== snap.sigmaMin) body.sigma_min_pct = now.sigmaMin;
  if (now.sigmaMax !== snap.sigmaMax) body.sigma_max_pct = now.sigmaMax;
  return body;
}

/* 窗口容量警示（纯展示、不阻断保存）：
   · 服务端窗口异常提示（window_fallback_text，正常为空串）；
   · 缓冲超过窗口 20% 单边上限 → 保存时会被夹取；
   · 扣除掐头去尾与间隔×账号数后不足窗口 → 部分账号可能签不上。 */
export function scheduleWarnText(opts) {
  var fallbackText = opts && opts.fallbackText ? String(opts.fallbackText) : "";
  var f = Number(opts && opts.frontSec) || 0;
  var b = Number(opts && opts.backSec) || 0;
  var gap = clampGap(opts && opts.gap);
  var win = Number(opts && opts.winSec) || 0;
  var n = Number(opts && opts.n) || 0;
  var maxMin = Number(opts && opts.maxMin) || 0;
  var msgs = [];
  if (fallbackText) msgs.push(fallbackText);
  if (win > 0 && (f > maxMin * 60 + 1e-9 || b > maxMin * 60 + 1e-9)) {
    msgs.push("缓冲超过窗口的 20%（单边上限 " + maxMin + " 分钟），保存时会被自动收缩");
  }
  if (win > 0) {
    var need = f + b + Math.max(gap, 0) * Math.max(n, 1);
    if (need > win) {
      msgs.push("按当前账号数（" + n + "）与间隔估算需要约 " + Math.ceil(need / 60) +
        " 分钟，超过窗口 " + Math.round(win / 60) + " 分钟，部分账号可能签不上");
    }
  }
  return msgs.join("；");
}

/* μ/σ 区间下限不小于上限的就地预警（服务端按"告警 + 回退默认"处理，前端不硬拦）。 */
export function distWarnText(lo, hi, kind) {
  var d = SCHEDULE_DEFAULTS;
  var fb = kind === "sigma" ? (d.sigmaMin + "~" + d.sigmaMax) : (d.muMin + "~" + d.muMax);
  return lo >= hi ? "下限不小于上限，将按默认 " + fb + " 生效" : "";
}

/* =========================================================================
   正态分布数学（settings-dist-viz 峰尖拖拽）
   ========================================================================= */

/* 拖拽映射的 σ 上限（%）：贴着 σ_eff 的窗口/3 封顶。 */
export var E_SIGMA_MAX = 33;

export function clampPct(v) { return Math.min(100, Math.max(0, Math.round(v))); }

export function pdf(x, mu, sigma) {
  var s = Math.max(sigma, 0.5);
  return Math.exp(-((x - mu) * (x - mu)) / (2 * s * s)) / (s * Math.sqrt(2 * Math.PI));
}

/* 区间盒上包络：固定 x、μ 时对 σ 求导的最优 σ=|x−μ| 夹到区间即闭式解，μ 按 25 点网格。 */
export function envAt(xMin, s) {
  var muL = s.effLo + s.span * s.muLo / 100, muH = s.effLo + s.span * s.muHi / 100;
  var sL = s.span * s.sgLo / 100, sH = s.span * s.sgHi / 100;
  var best = 0;
  for (var i = 0; i <= 25; i++) {
    var mu = muL + (muH - muL) * i / 25;
    var sg = Math.min(Math.max(Math.abs(xMin - mu), sL), sH);
    var v = pdf(xMin, mu, sg);
    if (v > best) best = v;
  }
  return best;
}

export function muMidPct(s) { return (s.muLo + s.muHi) / 2; }
export function sgMidPct(s) { return (s.sgLo + s.sgHi) / 2; }

/* 账号数超过 20 后实际散布被放大（σ_eff）；≤20 不放大。 */
export function sigmaFactor(n) {
  n = Math.max(1, Math.floor(n) || 1);
  return n > 20 ? 1 + Math.log2(n / 20) : 1;
}

export function sigmaCap(span) { return Math.max(1, Math.floor(span / 3)); }

export function fmtT(min) {
  min = Math.round(min);
  var h = Math.floor(min / 60), m = min % 60;
  return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
}

/* 峰值区间 = 顶点 ± 半径：mid/rPct 任一变化都重算两个整数端点，区间不出窗。
   纯函数：返回新端点，由组件写回状态（legacy 里它直接写隐藏 input）。 */
export function applyMu(mid, rPct) {
  rPct = Math.min(rPct, mid, 100 - mid);
  var fullW = Math.max(2, Math.round(2 * rPct));
  var lo = clampPct(mid - fullW / 2);
  if (lo + fullW > 100) lo = 100 - fullW;
  return { muLo: lo, muHi: lo + fullW };
}

/* 底座端部/纵向拖拽：等比缩放 σ 区间（中点不变），钳 [1,100] 且保持间隙。 */
export function applySgScale(sgLo, sgHi, k) {
  var nLo = clampPct(sgLo * k), nHi = clampPct(sgHi * k);
  if (nHi <= nLo) nHi = nLo + 1;
  if (nHi > 100) { nLo = Math.max(1, Math.round(nLo * 100 / nHi)); nHi = 100; }
  if (nLo < 1) nLo = 1;
  return { sgLo: Math.min(nLo, nHi - 1), sgHi: Math.max(nHi, Math.min(nLo, nHi - 1) + 1) };
}

/* 散布编辑器：±分钟独立设置，钳 [0,100] 并保持 hi > lo ≥ 1。 */
export function applySgBounds(loPct, hiPct) {
  var a = clampPct(loPct), b = clampPct(hiPct);
  a = Math.max(1, a);
  b = Math.max(a + 1, b);
  return { sgLo: a, sgHi: b };
}

/* =========================================================================
   公告（双人发布）
   ========================================================================= */

/* 后端禁换行：前端在输入阶段就把换行族换成空格，提交前拦住（避免提交后才 400）。 */
export function sanitizeAnnouncement(v) {
  return String(v == null ? "" : v).replace(/[\r\n\u2028\u2029]+/g, " ");
}

export function annMeta(by, at) {
  var parts = [];
  if (by) parts.push("由 " + by);
  if (at) parts.push(at);
  return parts.join(" · ");
}

export function annDraftMetaText(draft, by, at) {
  var meta = annMeta(by, at);
  return draft ? ("草稿" + (meta ? "：" + meta : "已保存")) : "（无草稿）";
}

/* 发布按钮三态（按**当前草稿**判，含未保存的编辑）：非空→发布；空且线上有内容→下线；
   两者皆空→noop（禁用，与后端 400 同口径，前端先挡住误按）。 */
export function annPublishState(currentText, onlineText) {
  var cur = String(currentText || "").trim();
  var online = String(onlineText || "");
  return { offline: !cur && !!online, noop: !cur && !online };
}

/* =========================================================================
   消息推送
   ========================================================================= */

/* 计数口径 = 「已用 X/上限 Y（剩 R）」：额度是被一条条**占用**吃掉的，占用发生在发送
   之前——只显余额时"从未发出却没退成"的占用完全隐形。 */
export function quotaPart(name, max, remaining) {
  if (remaining == null) return name + " 不限";
  var m = Number(max) || 0;
  var r = Number(remaining) || 0;
  if (m <= 0) return name + " 不限";
  return name + " 已用 " + Math.max(0, m - r) + "/" + m + "（剩 " + r + "）";
}

/* 推送状态行。额度余量按 quota_visible 分支：后端对无权查看者把 remaining 置 null，而
   null 的正常语义是"上限为 0（不限）"——两者恰好相反，只按 null 判会把"无权查看"显示成
   "不限"。 */
export function notifyStatusText(data) {
  data = data || {};
  var parts = [];
  if (data.enabled) {
    parts.push("已开启（" + (data.type === "serverchan" ? "Server酱" : "自定义地址") +
      "，密钥 " + (data.secret_masked || "已配置") + "）");
    if (data.urgent_only) parts.push("仅推送重要告警");
  } else {
    parts.push(data.configured ? "已配置但不可用（密钥缺失或解密失败，请重新填写密钥）" : "未配置");
  }
  if (data.quota_visible === false) {
    parts.push("今日额度：仅主管理员可见");
  } else {
    parts.push("今日额度：" + quotaPart("非紧急", data.daily_max, data.daily_remaining) +
      " / " + quotaPart("紧急", data.urgent_daily_max, data.urgent_daily_remaining) +
      "（已用按占用计数：占用先于发送，未必等于已送达）");
  }
  return parts.join("；");
}

export function notifySnapshot(data) {
  data = data || {};
  return {
    type: data.type || "",
    cooldown: data.cooldown != null ? Number(data.cooldown) : null,
    urgent_only: !!data.urgent_only,
    daily_max: data.daily_max != null ? Number(data.daily_max) : null,
    urgent_daily_max: data.urgent_daily_max != null ? Number(data.urgent_daily_max) : null,
    configured: !!data.configured,
  };
}

/* 注意：推送/邮件的字段不是 /api/settings 的档位键，故这里刻意用 `payload` 而不是
   `body`——档位对拍测试按 `body.<键>` 抽取设置写键，用 `payload` 可避免把通知通道的
   协议字段误判成"未登记的设置键"。 */
export function notifyBody(snap, form) {
  var payload = {};
  var t = form.type || "";
  var secret = String(form.secret || "").trim();
  var cd = parseInt(form.cooldown, 10);
  var dm = parseInt(form.dailyMax, 10);
  var um = parseInt(form.urgentMax, 10);
  var ur = !!form.urgentOnly;
  if (t !== snap.type) payload.type = t;
  if (secret) payload.secret = secret;
  if (!isNaN(cd) && cd !== snap.cooldown) payload.cooldown = Math.max(0, cd);
  if (ur !== snap.urgent_only) payload.urgent_only = ur;
  if (!isNaN(dm) && dm !== snap.daily_max) payload.daily_max = Math.max(0, dm);
  if (!isNaN(um) && um !== snap.urgent_daily_max) payload.urgent_daily_max = Math.max(0, um);
  return payload;
}

/* =========================================================================
   邮件通知 / SMTP
   ========================================================================= */

/* 含打码串/占位串的输入一律按空处理（否则按字面落盘会损坏配置）。 */
export function clean(v) {
  var s = String(v || "").trim();
  return (!s || s.indexOf("*") !== -1 || s.charAt(0) === "<") ? "" : s;
}

/* 稳定 id：形状与后端校验（^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$）同口径。
   条目身份是 id 而不是数组位置——"留空沿用"与"改 host 不带走旧授权码"都靠它。 */
export function newSmtpId() {
  var r = "";
  while (r.length < 12) {
    r += Math.random().toString(36).slice(2);
  }
  return "smtp-" + r.slice(0, 12).replace(/[^a-z0-9]/g, "0");
}

export function smtpRowId(entry) {
  return (entry && entry.id) ? String(entry.id) : newSmtpId();
}

/* 从服务端 smtps[] 构造可编辑行（非敏感字段回填、脱敏字段只作 placeholder）。
   脱敏字段**绝不回填 value**：回填会让保存把打码串当真实值落盘，损坏配置。 */
export function smtpRowsFrom(list) {
  return (list || []).map(function (e) {
    return {
      id: smtpRowId(e),
      host: String(e.host || ""),
      port: Number(e.port || 465),
      user0: String(e.user || "留空沿用"),
      has_pass: !!e.has_pass,
      // 可编辑值：host/port 回填；user/pass 恒空（脱敏值只作 placeholder）
      user: "",
      pass: "",
    };
  });
}

/* 提交：id 随行不随位（提交顺序变了，后端仍按 id 找回各自的旧凭据）。 */
export function collectSmtps(rows) {
  return (rows || []).map(function (row) {
    return {
      id: String(row.id || ""),
      host: clean(row.host),
      port: parseInt(row.port, 10) || 465,
      user: clean(row.user),
      pass: String(row.pass || "").trim(),
    };
  });
}

/* 目标漂移提示：改了 host/端口 = 换中继，后端对这种行**不会**沿用旧授权码。
   "留空沿用"的占位文案此刻就成了谎话，必须当场改口。纯函数：给定行与当前 host/port
   返回该行 pass/user 的 placeholder。 */
export function driftPlaceholder(row, hostValue, portValue) {
  var drifted = String(hostValue || "").trim() !== String(row.host || "") ||
    String(portValue) !== String(row.port || 465);
  if (row.has_pass && drifted && !String(row.pass || "")) {
    return {
      pass: "服务器已更换，旧授权码不再沿用，请重新输入",
      user: "服务器已更换，如需换发件账号请重填",
    };
  }
  return {
    pass: row.has_pass ? "已配置，留空沿用" : "未配置",
    user: row.user0 || "留空沿用",
  };
}

/* 邮件状态行：开关"已开启"不等于告警能送达——发信清单为空时这一路是哑的，
   状态行必须自己说破（否则只剩表格占位行一句小字，开关读着"已开启"极易漏看）。 */
export function mailStatusText(data) {
  data = data || {};
  var smtpEmpty = !(data.smtps && data.smtps.length);
  if (data.enabled) {
    return "已开启 · 发件 " + (data.user || "未配置") + " · 告警收件 " + (data.admin_to || "未配置") +
      (smtpEmpty ? " · 注意：无发信 SMTP，告警邮件一封都发不出去" : "");
  }
  return smtpEmpty ? "未开启（未配置发件 SMTP）" : "未开启（已配置发件 SMTP，可由主管理员开启）";
}

export function mailSnapshot(data) {
  data = data || {};
  var toShown = String(data.admin_to || "");
  return { enabled: !!data.enabled, hasTo: !!toShown && toShown.charAt(0) !== "<" };
}

/* 只提交真正变化的键：开关单独变时不重写 SMTP 列表（避免把未改动的行也落盘一遍）。 */
export function mailBody(snap, form, tableDirty) {
  var payload = {};
  if (form.enabled !== snap.enabled) payload.enabled = !!form.enabled;
  var toVal = String(form.adminTo || "").trim();
  if (toVal) payload.admin_to = toVal;
  if (tableDirty) payload.smtps = collectSmtps(form.smtps);
  return payload;
}

/* =========================================================================
   容量配额
   ========================================================================= */

export function quotaSnapshot(data) {
  var cap = (data && data.capacity) || {};
  return { users: Number(cap.users_max) || 0, accounts: Number(cap.accounts_max) || 0 };
}

export function quotaBody(snap, form) {
  var body = {};
  var u = parseInt(form.users, 10);
  var a = parseInt(form.accounts, 10);
  if (isNaN(u)) u = snap.users;
  if (isNaN(a)) a = snap.accounts;
  if (u !== snap.users) body.max_users = u;
  if (a !== snap.accounts) body.max_accounts = a;
  return body;
}

/* 容量建议只有一行摘要；没有实测值就明说"未实测"并指向写入处，不是让用户点按钮硬凑数字。 */
export function adviceLine(d) {
  var win = (d && d.window) || {};
  var rec = (d && d.recommendation) || null;
  var measured = (d && d.measured) || null;
  function count(v) { return Number(v) || 0; }
  if (!measured) {
    return "未实测：配置文件里还没有实测容量，因此不给出建议值；可先点右上角「测试单账号耗时」粗量一次，再由部署者写入配置文件。";
  }
  return "建议并行执行体数 " + (rec ? count(rec.executors_needed) + " 个" : "—")
    + "（每个约 " + (rec ? count(rec.per_executor_accounts) : "—") + " 个账号，含慢账号余量）"
    + " · 实测容量 " + count(measured.per_executor_capacity) + " 个"
    + " · 计入容量 " + count(d && d.current_accounts) + " 个账号"
    + (win.start && win.end ? " · 有效窗口 " + win.start + " ~ " + win.end : "");
}

export function clockText(sec) {
  if (sec < 60) return Math.max(1, Math.round(sec)) + " 秒";
  var m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m + " 分" + (s ? " " + s + " 秒" : "");
}

/* =========================================================================
   健康与探针
   ========================================================================= */

export function healthSnapshot(data) {
  data = data || {};
  return {
    verify: data.account_verify ? 1 : 0,
    probe: data.probe_enable ? 1 : 0,
    // 服务端返回的探针时间同样过 `norm`：形状与 00:00–23:59 范围同一处校验，
    // 越界值（"25:00"）fail-closed 回退默认，避免显示值/落盘值/生效值三段不等。
    time: norm(data.probe_time, "20:00"),
    interval: String(data.probe_interval || "1"),
  };
}

export function healthBody(snap, form) {
  var now = {
    verify: form.verify ? 1 : 0,
    probe: form.probe ? 1 : 0,
    time: String(form.time || "20:00").slice(0, 5),
    interval: String(form.interval || "1"),
  };
  var body = {};
  if (now.verify !== snap.verify) body.account_verify = now.verify;
  if (now.probe !== snap.probe) body.probe_enable = now.probe;
  if (now.time !== snap.time) body.probe_time = now.time;
  if (now.interval !== snap.interval) body.probe_interval = now.interval;
  return body;
}

/* =========================================================================
   系统开关（按变更方向分权）
   ========================================================================= */

/* 系统开关两个字段的字面量（键名单源是后端 `web/app.py` 的 GLOBAL_PAUSE_KEY 与
   `registration_pause`）。列在这里供档位对拍静态读取——`switchesBody` 是动态键写法
   `body[field] = …`，只靠它认不出这两个键。 */
export var SWITCH_FIELDS = ["global_pause", "registration_pause"];

/* 这个方向当前会话能不能做（与后端 api_settings_save 的方向判定同口径）：
   急停（0→1）任意管理员；恢复（1→0）与注册开关两方向仅主管理员。 */
export function canDo(field, next, isMaster) {
  if (field === "global_pause" && !next) return !!isMaster;
  if (field !== "global_pause") return !!isMaster;
  return true;
}

/* 禁用原因**按按钮**取：急停那颗永不被禁，所以走这里的只有"恢复签到"与注册开关。 */
export function whyFor(field) {
  return field === "global_pause" ? "恢复自动签到仅主管理员可做" : "注册开关仅主管理员可做";
}

export function pauseHintText(state) {
  var parts = [];
  if (state && state.globalPause) parts.push("签到当前处于暂停状态 — 自动签到不会执行，直至手动恢复");
  if (state && state.regPause) parts.push("注册当前处于暂停状态 — 新用户无法自助注册");
  return parts.join("；");
}

export function switchesBody(field, next) {
  var body = {};
  body[field] = next ? 1 : 0;
  return body;
}
