/**
 * 数据看板口径层（**刻意保留纯 JS**，不是 TS）。
 *
 * 为什么不用 TS：`tests/test_dashboard_stats_caliber_js.py` 会用 `_extract_function` /
 * `_extract_object` 从本文件里按花括号配对抽出 `statusKind` / `normalizeDaily` /
 * `acceptAccounts` / `rateOf` 四段并**在 Node 里真跑**（迁移前它们扫的是 legacy 的
 * `web/static/js/pages/data_dashboard.js`），另抽 `trendView` / `distView` /
 * `calendarView` / `rateKpiView` 做静态口径钉。抽函数靠字面量 `"function " + 名字 + "("`
 * 定位，故：
 *   · 必须保持标准具名 function 声明写法（不加 TS 类型注解、不改成箭头函数、参数不带
 *     对象默认值解构）——三者都会让抽出的文本在 Node 里跑不起来或配错括号；
 *   · **本注释块内不得出现那四个字面量本身**（即"function 加名字加左括号"的连写）；
 *     抽取器先命中注释里的那次出现，就会从注释处数括号、永远配不平；
 *   · 被抽函数体内（含其中注释）不得出现不成对的 `{` / `}`——抽取器只数花括号，字符串/
 *     注释里的裸花括号同样计入；也不要在字符串里写会被当成形态说明的花括号。
 *   · 改动实现前先读那个测试。它只认字面量，被格式化即报错（失效方向是红，不是假绿）。
 *
 * 本模块**零 DOM、零网络、零全局**：所有函数是数据 → 数据的纯变换。图表实例的创建/
 * 更新/销毁、日历换月动效、请求编排留在 `DashboardPage.vue`。
 *
 * ## 双口径（MF-55）是本模块存在的理由
 * 事件数与账号数是两个**不可互换**的口径：
 *   · 事件（尝试）= `daily_stats.row_cnt`，在 `normalizeDaily` 里按天打桶、跨天相加仍是
 *     事实；趋势图与成功率的分母都用它；
 *   · 账号 = `/api/admin/sign-events` 的 `accounts_stats` **后端终值**（窗口去重 total /
 *     按状态去重 by_status / 按日终态 by_day），本模块一律透传，**绝不跨天或跨状态直加**。
 * 前端不聚合 `cnt`（页面拿不到 phone 明细、无从去重）——「953 vs 真值 94」正是直加出来的。
 *
 * ## 词表
 * `statusKind` 对状态码做**穷举**分类：成功 / 失败 / 已知跳过 / 未知（unknown）。词表外的
 * 未知码显式成 `unknown` 桶并上屏（KPI 副文案 / 趋势 / 日历 title），不静默落 skip——
 * 落 skip 会让枚举膨胀时的成功率只抬不降。图表短名取自前端唯一事实源
 * `lib/status-vocab.js`（`STATUS_VOCAB[*].short`）；本表的**键集合**由
 * `tests/test_yiban_status_single_source.py` 钉到唯一事实源 `yiban.status.ALL_STATUSES`。
 */

import { STATUS_VOCAB } from "../lib/status-vocab.js";

/** 签到事件请求路径（唯一定义处）。`stage=sign` 是把探针事件排除在统计外的**唯一防线**
 *  （探针同样写 success/failed），故整体作为字面量常量，被守卫钉死。 */
export var SIGN_EVENTS_PATH = "/api/admin/sign-events?days=30&stage=sign";

/** 事件窗口天数（与 URL 的 days=30 同口径）。 */
export var SIGN_DAYS = 30;

/** 默认的「签到热力图」脚注文案（服务端经 window.YB_DASHBOARD_STATE 下发可覆盖）。 */
export var DEFAULT_CAL_NOTE = "真实数据（不含探针），覆盖近 30 天";

/** 热力图星期表头（周一起始，顺序即列序）。 */
export var WEEK = ["一", "二", "三", "四", "五", "六", "日"];

export var SUCCESS_ST = { success: 1, already: 1 };
export var FAIL_ST = { failed: 1 };

/** 状态码 → 图表短名（取自 lib/status-vocab.js 的 `short`，来源唯一；
 *  键集合被守卫钉死）。 */
export var STATUS_LABEL = pluckShort(STATUS_VOCAB);

function pluckShort(vocab) {
  var out = {};
  for (var k in vocab) out[k] = vocab[k].short;
  return out;
}

/** 状态码 → 主题令牌名（颜色值一律从 CSS 自定义属性取，主题切换时重取）。 */
export var STATUS_TOKEN = {
  success: "success", already: "success", failed: "danger", retrying: "warning",
  no_task: "info", no_position: "purple", paused: "warning",
  user_cancelled: "muted", skipped_window: "light", skipped_norange: "light",
  pending: "light", global_paused: "muted",
};

/** 空看板状态工厂（Vitest 与组件共用同一形状）。 */
export function emptyState() {
  return {
    dailyMap: {}, dailyDays: [], accountsTotal: 0, statusAccounts: {}, dayFinal: {},
    calMonth: null, signLoaded: false, signFailed: false, slots: [],
    weekendKnown: false, satSign: true, sunSign: true,
  };
}

export function pad2(n) {
  return ("0" + n).slice(-2);
}

export function fmtDate(d) {
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}

export function fmtMonth(d) {
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1);
}

function addDays(d, delta) {
  var n = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  n.setDate(n.getDate() + delta);
  return n;
}

/** 千分位本地化；非有限数回落 "0"（与 legacy num 逐字一致）。 */
export function num(n) {
  n = Number(n);
  return isFinite(n) ? n.toLocaleString("zh-CN") : "0";
}

/* ---------------- 状态词表 ---------------- */

export function statusKind(st) {
  st = String(st || "");
  if (SUCCESS_ST[st]) return "success";
  if (FAIL_ST[st]) return "fail";
  if (Object.prototype.hasOwnProperty.call(STATUS_LABEL, st)) return "skip";
  return "unknown";
}

export function statusLabel(st) {
  return STATUS_LABEL[st] || String(st || "未知");
}

export function statusColor(st, t) {
  return t[STATUS_TOKEN[st] || "light"] || t.light;
}

/* ---------------- 聚合：事件数与账号数双口径 ---------------- */

/**
 * 事件口径：`daily_stats`（按 (day,status) 聚合）按天打桶。
 *
 * 只读 `row_cnt`（原始行数）——行数跨维相加仍是事实。**刻意不读 `cnt`**：`cnt` 按
 * (day,status) 各自去重、桶间不互斥，跨天或跨状态直加必虚增（MF-55 的 953 vs 94）。
 * 返回 `{ map, days }`，不再就地改全局长态（纯函数，可单测）。
 */
export function normalizeDaily(rows) {
  var map = {};
  (rows || []).forEach(function (r) {
    var day = String((r && r.day) || "");
    if (!day) return;
    var m = map[day] || (map[day] = { success: 0, fail: 0, skip: 0, unknown: 0, total: 0 });
    var ev = Number(r.row_cnt) || 0;
    var kind = statusKind(String(r.status || ""));
    m[kind] += ev;
    m.total += ev;
  });
  return { map: map, days: Object.keys(map).sort() };
}

/**
 * 账号口径：后端窗口去重终值（`accounts_stats`）原样透传。
 *
 *   total        —— 窗口内按 phone 去重的账号数，「签到账号总数」的唯一来源；
 *   by_status    —— 每状态窗口去重账号数（桶间**可有交集**，占比以桶合计为分母）；
 *   by_day[day]  —— 日 × **最终状态** 分桶：每账号当日最后一条事件恰落一桶，桶互斥
 *                   ⇒ 日内相加合法；日历按最终态染色（先败后成的日子不涂红）。
 */
export function acceptAccounts(a) {
  a = a || {};
  var byDay = {};
  (a.by_day || []).forEach(function (r) {
    var day = String((r && r.day) || "");
    if (!day) return;
    var m = byDay[day] || (byDay[day] = { success: 0, fail: 0, skip: 0, unknown: 0, total: 0 });
    var n = Number(r.accounts) || 0;
    var kind = statusKind(String(r.status || ""));
    m[kind] += n;
    m.total += n;
  });
  return { total: Number(a.total) || 0, byStatus: a.by_status || {}, dayFinal: byDay };
}

/**
 * 成功率 = 成功 ÷（成功 + 失败）。跳过与未知都**不进分母**（未定论），
 * 未知同样不进分子——它单列成桶并上屏。
 */
export function rateOf(bucket) {
  if (!bucket) return null;
  var att = bucket.success + bucket.fail;
  return att > 0 ? Math.round(bucket.success / att * 1000) / 10 : null;
}

/**
 * 「今日无结果」文案：区分「当天本来就不签到」与「还没产生结果」。
 * 周六/周日签到可在系统设置中关闭，关闭时显示「今日暂无结果」会误导管理员以为调度异常。
 */
export function noResultText(state, now) {
  if (state.weekendKnown) {
    var dow = now.getDay();
    if (dow === 6 && state.satSign === false) return "今日不签到（周六签到已关闭）";
    if (dow === 0 && state.sunSign === false) return "今日不签到（周日签到已关闭）";
  }
  return "今日暂无签到结果";
}

/* ---------------- KPI 视图 ---------------- */

/** 容量文案（账号口径：`cur` = 会签到的账号数，未通过审核的行不计容量）。 */
export function capacityText(cur, max, breakdown, audit) {
  var parts = [];
  if (breakdown) {
    parts.push("正常 " + (breakdown.normal || 0));
    parts.push("用户暂停 " + (breakdown.user_paused || 0));
    parts.push("账密暂停 " + (breakdown.cred_paused || 0));
  }
  var out = String(cur) + (Number(max) > 0 ? " / " + max : " / 不限");
  if (parts.length) out += "（" + parts.join(" · ") + "）";
  if (Number(audit) > 0) out += "；未通过审核 " + audit + " 个未计入";
  return out;
}

/** 容量卡三行文案；`!d` 返回 null（组件渲染「加载失败」）。 */
export function capacityRows(d) {
  if (!d) return null;
  var c = d.capacity || {};
  var e = d.capacity_estimate || {};
  return {
    accounts: capacityText(c.accounts, c.accounts_max, c.accounts_breakdown, c.accounts_audit),
    users: capacityText(c.users, c.users_max, null, null),
    estimate: "容量 " + (e.accounts_cap != null ? e.accounts_cap : "—")
      + " · 当前 " + (e.current_accounts != null ? e.current_accounts : "—")
      + " · 潜在 " + (e.potential_load != null ? e.potential_load : "—"),
  };
}

/**
 * 今日成功 / 失败账号 KPI（账号口径）。
 *
 * 与「今日成功率」的事件口径不同：这里读账号口径的日终态分桶 `dayFinal[today]`
 * —— 每账号当日最终状态恰落一桶、桶互斥，故「成功账号数」不会因同一账号重试多次而虚增。
 * 之所以不用「活跃账号 / 注册用户」占这两张卡：那两个数来自 /api/settings 的容量区块，
 * 与下方「账号与用户容量」卡同源同数，一屏内各出现一遍，纯重复（容量数字只在容量卡出现）。
 * 返回 `{ success, fail }` 两个 KPI 视图；数据未回返 null（保持骨架），sign 失败给错误视图。
 */
export function todayAccountKpiViews(state, now) {
  var CALIBER = "账号口径：当日最终状态，每账号恰计一次（重试不重复计数）；"
    + "与「今日成功率」的签到事件口径（重试多次分别计数）不同";
  if (state.signFailed) {
    var err = {
      failed: true, value: "—", sup: null, sub: "签到事件加载失败",
      subTitle: "", valueTitle: "", pill: null, alert: false,
    };
    return { success: err, fail: err };
  }
  if (!state.signLoaded) return null;
  var today = fmtDate(now);
  var fin = state.dayFinal[today] || { success: 0, fail: 0, skip: 0, unknown: 0, total: 0 };
  return {
    success: {
      failed: false, value: num(fin.success), sup: null,
      sub: "按账号统计 · 重试不重复计数", subTitle: CALIBER, valueTitle: CALIBER,
      pill: null, alert: false,
    },
    fail: {
      failed: false, value: num(fin.fail), sup: null,
      sub: "按账号统计 · 重试不重复计数", subTitle: CALIBER, valueTitle: CALIBER,
      pill: null, alert: false,
    },
  };
}

/**
 * 今日成功率 KPI。返回 null 表示「数据尚未回来，保持骨架」；签到事件失败时返回**失败视图**
 * （错误行），这样设置接口后到触发的重算不会把它盖成「今日暂无签到结果」（失败 ≠ 没有结果）。
 */
export function rateKpiView(state, now) {
  if (state.signFailed) {
    return {
      failed: true, empty: false, value: "—", sup: null,
      sub: "签到事件加载失败", subCls: "", subTitle: "", valueTitle: "", pill: null,
    };
  }
  if (!state.signLoaded) return null;
  var today = fmtDate(now), y = fmtDate(addDays(now, -1));
  var m = state.dailyMap[today], yb = state.dailyMap[y];
  var rt = rateOf(m), ry = rateOf(yb);
  if (rt == null) {
    return {
      empty: true, value: "—", sup: null, subTitle: "", valueTitle: "",
      sub: noResultText(state, now), subCls: "dash-muted", pill: null,
    };
  }
  var sub = "成功 " + num(m.success) + " · 失败 " + num(m.fail)
    + (m.skip > 0 ? " · 跳过 " + num(m.skip) : "")
    + (m.unknown > 0 ? " · 未知状态 " + num(m.unknown) : "");
  var pill;
  if (ry != null) {
    var diff = Math.round((rt - ry) * 10) / 10;
    pill = {
      text: "较昨日 " + (diff > 0 ? "+" : "") + diff + "%",
      cls: diff > 0 ? "up" : diff < 0 ? "down" : "flat",
      title: "与昨日签到成功率的变化",
    };
  } else {
    pill = { text: "无昨日对比", cls: "flat", title: "" };
  }
  return {
    empty: false, value: rt.toFixed(1), sup: "%",
    subTitle: "",
    valueTitle: "成功率 = 成功 ÷（成功 + 失败），跳过与未知不计入；按事件（尝试）计，重试各算一次",
    sub: sub, subCls: "", pill: pill,
  };
}

/** 待处理账号 KPI：待审核 + 已拒绝（单一口径不叠加；`deleted` 行不计）。 */
export function pendingKpiView(d) {
  if (!d) {
    return { failed: true, value: "—", sup: null, sub: "待处理账号加载失败", subTitle: "", valueTitle: "", pill: null, alert: false };
  }
  var list = (d && d.accounts) || [];
  var pending = 0, rejected = 0;
  list.forEach(function (a) {
    if (!a || a.deleted) return;
    if (a.status === "pending") pending += 1;
    else if (a.status === "rejected") rejected += 1;
  });
  var total = pending + rejected;
  return {
    failed: false, value: num(total), sup: null,
    sub: "待审核 " + num(pending) + " · 已拒绝 " + num(rejected),
    subTitle: "", valueTitle: "", pill: null, alert: total > 0,
  };
}

/* ---------------- 图表视图（数据 → 数据；绘制在组件里） ---------------- */

/**
 * 趋势视图：堆叠柱状，回答「发生多少次」，取**事件（行数）**桶。
 * 未知码单列一条「未知」序列（只在真出现时进图例）。`state.dailyDays` 为空时返回 null。
 */
export function trendView(state) {
  var days = state.dailyDays;
  if (!days.length) return null;
  var labels = [], ok = [], fail = [], skip = [], unk = [], sum = { ok: 0, fail: 0, skip: 0, unk: 0 };
  days.forEach(function (day) {
    var ev = state.dailyMap[day];
    labels.push(day.slice(5));
    ok.push(ev.success); fail.push(ev.fail); skip.push(ev.skip); unk.push(ev.unknown);
    sum.ok += ev.success; sum.fail += ev.fail; sum.skip += ev.skip; sum.unk += ev.unknown;
  });
  var datasets = [
    { label: "成功", data: ok, token: "success" },
    { label: "失败", data: fail, token: "danger" },
    { label: "跳过", data: skip, token: "warning" },
  ];
  if (sum.unk > 0) datasets.push({ label: "未知", data: unk, token: "muted" });
  var total = sum.ok + sum.fail + sum.skip + sum.unk;
  var meta = [
    ["近 30 天成功", num(sum.ok)],
    ["近 30 天失败", num(sum.fail)],
    ["近 30 天跳过", num(sum.skip)],
    ["日均签到事件（次）", num(days.length ? Math.round(total / days.length * 10) / 10 : 0)],
  ];
  if (sum.unk > 0) meta.splice(3, 0, ["近 30 天未知", num(sum.unk)]);
  return { labels: labels, datasets: datasets, meta: meta };
}

/**
 * 分布视图：回答「涉及多少账号」，取值直接读**后端窗口去重终值** `statusAccounts`。
 * 占比分母是桶合计（桶间可有交集），但「签到账号总数」一律读 `accountsTotal`，
 * 绝不拿桶合计冒充。无正数桶时返回 null。
 */
export function distView(state) {
  var by = state.statusAccounts;
  var keys = Object.keys(by).filter(function (k) { return Number(by[k]) > 0; });
  keys.sort(function (a, b) { return by[b] - by[a]; });
  if (!keys.length) return null;
  return {
    labels: keys.map(statusLabel),
    data: keys.map(function (k) { return by[k]; }),
    tokens: keys.map(function (k) { return STATUS_TOKEN[k] || "light"; }),
    meta: [["签到账号总数", num(state.accountsTotal)], ["结果类型", num(keys.length)]],
  };
}

/** 自选时间片视图：横向柱状，自选人数 vs 该时段人数上限。空列表返回 null。 */
export function slotsView(slots) {
  if (!slots.length) return null;
  var total = slots.reduce(function (n, s) { return n + (Number(s.count) || 0); }, 0);
  var top = slots.slice().sort(function (a, b) {
    return (Number(b.count) || 0) - (Number(a.count) || 0);
  })[0];
  return {
    labels: slots.map(function (s) { return String(s.label || ""); }),
    counts: slots.map(function (s) { return Number(s.count) || 0; }),
    caps: slots.map(function (s) { return Number(s.cap) || 0; }),
    coverage: "共 " + slots.length + " 个可选时段",
    meta: [
      ["自选人数合计", num(total)],
      // 「最热门」只在有人自选时成立：非空列表首片恒为 top（即使 count 全 0），
      // 旧写法会给出「07:00（0 人）」这种没有定义的结论；count<=0 一律回落 "—"。
      ["最热门时段", top && Number(top.count) > 0
        ? String(top.label || "") + "（" + num(top.count) + " 人）"
        : "—"],
    ],
  };
}

/**
 * 签到热力图：周一起始、展示桶取后端**日终态分桶** `dayFinal`（每账号当日恰落一桶、
 * 桶互斥），存在性以事件行数 `dailyMap` 兜底（两者取或，防换口径误判空）。
 * 返回标签与格子数据（类名 / title 是口径，markup 由 Vue 出）。
 */
export function calendarView(state, opts) {
  var now = opts.now;
  var base = opts.month ? new Date(opts.month + "-01T00:00:00") : new Date(now.getFullYear(), now.getMonth(), 1);
  var today = fmtDate(now);
  var y = base.getFullYear(), mo = base.getMonth();
  var lead = (new Date(y, mo, 1).getDay() + 6) % 7;
  var daysIn = new Date(y, mo + 1, 0).getDate();
  var trail = (7 - ((lead + daysIn) % 7)) % 7;
  var cells = [];
  var i;
  for (i = 0; i < lead; i++) cells.push({ other: true });
  for (i = 1; i <= daysIn; i++) {
    var ds = fmtDate(new Date(y, mo, i));
    var fin = state.dayFinal[ds];
    var ev = state.dailyMap[ds];
    var hasData = !!((((fin && fin.total) || (ev && ev.total)) || 0) > 0);
    var cls = "mini-cal-day";
    if (ds > today) cls += " is-future";
    else if (hasData) {
      var f = fin || { success: 0, fail: 0 };
      cls += f.fail > 0 ? " is-fail" : (f.success > 0 ? " is-ok" : " is-none");
    } else cls += " is-none";
    if (ds === today) cls += " is-today";
    var title = !hasData
      ? (ds > today ? "未来日期（尚未签到）" : "当日无真实签到记录")
      : (fin && fin.total > 0
        ? "成功 " + fin.success + " · 失败 " + fin.fail + " · 跳过 " + fin.skip
          + (fin.unknown > 0 ? " · 未知 " + fin.unknown : "") + "（账号，当日最终态）"
        : "有签到记录（账号未识别）");
    cells.push({ d: i, cls: cls, title: title });
  }
  for (i = 0; i < trail; i++) cells.push({ other: true });
  return { label: y + " 年 " + (mo + 1) + " 月", cells: cells };
}

/* ---------------- 运行状态视图 ---------------- */

/** 暂停徽标：`paused ? labels[0] : labels[1]`，tone 为 bad/ok。 */
export function pauseView(d) {
  if (!d) return null;
  function badge(paused, labels) {
    return { text: paused ? labels[0] : labels[1], tone: paused ? "bad" : "ok" };
  }
  return {
    global: badge(!!Number(d.global_pause), ["全局暂停中", "正常运行"]),
    reg: badge(!!Number(d.registration_pause), ["注册已暂停", "注册开放"]),
  };
}

/**
 * 服务器时钟视图：偏移 <=5s 同步（ok）、<=60s 小偏差（warn）、否则 bad。
 * `nowMs` 由调用方传入（客户端 Date.now()），便于单测。返回 null 表示读取失败。
 */
export function clockView(d, nowMs) {
  if (!d || d.server_ts == null) return null;
  var drift = Math.round(Number(d.server_ts) - nowMs / 1000);
  var abs = Math.abs(drift);
  var cls = abs <= 5 ? "badge--ok" : abs <= 60 ? "badge--warn" : "badge--bad";
  var tz = Number(d.tz_offset_min) || 0;
  return {
    tone: cls,
    text: abs <= 5 ? "时钟已同步" : "偏差 " + (drift > 0 ? "+" : "") + drift + " 秒",
    detail: String(d.now || "") + "（UTC" + (tz >= 0 ? "+" : "") + (tz / 60) + "）",
  };
}

/** 公告视图：空文本回落「暂无公告」。 */
export function announcementView(d) {
  var text = String((d && d.text) || "").trim();
  if (!text) return { empty: true, text: "暂无公告" };
  return { empty: false, text: text };
}

/** 连通性检测结果视图（`reachable` → ok/bad + detail）。 */
export function pingView(d) {
  var ok = !!(d && d.reachable);
  return { ok: ok, text: ok ? "可达" : "不可达", detail: d && d.detail ? String(d.detail) : "" };
}
