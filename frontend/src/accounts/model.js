/* 账号管理页（管理端 /work/accounts）口径层——纯函数，无 DOM / 无网络 / 无全局。
   刻意用纯 JS（不是 TS）：`tests/test_work_accounts_selection.py` 会把 byPhone /
   selectedAccounts / selectedIds / selectedPhones 这组函数从本文件按花括号配对抽出、
   放进 node 真跑，钉「选中集以手机号为身份键、列表重排后不粘到邻居」这条不变量。
   该抽取只取**函数体本身**，故本文件顶部的 `import` 不影响它；但受抽取的函数体内
   不得出现 TS 注解 / 解构默认值（会让抽出的文本在 node 里跑不起来）。改动前先读那个测试。

   与 legacy `pages/work_accounts.js` + `components/account-table.js` 的关系：
   · 分组归类、检索匹配、选中解析、状态映射、展示文案、行菜单/批量动作集全部收到这里；
   · 组件 Accounts.vue 只负责渲染与把动作派发给 ops.js（写操作链路，同样纯 JS）。

   安全口径：列表项 phone / owner 已是服务端脱敏值；本层只消费脱敏串，完整号不经此处。 */

import { STATUS_VOCAB } from "../lib/status-vocab.js";

export var GROUP_KEYS = ["pending", "active", "deleted"];

/* 组定义：表格容器 id、检索输入 id、批量条 id、空态 id、计数 id 与文案。
   id 沿用 legacy（服务端模板时代的对外锚点），e2e / 契约测试按它们取值。 */
export var GROUPS = {
  pending: {
    key: "pending",
    title: "待处理账号",
    sub: "通过后参与定时签到；驳回必须填写理由（将通知提交者）。",
    searchLabel: "搜索待处理账号",
    tbody: "accounts-pending-tbody",
    empty: "accounts-pending-empty",
    count: "accounts-pending-count",
    bar: "batch-bar-pending",
    cnt: "batch-count-pending",
    all: "select-all-pending",
    emptyText: "已全部审核 · 今日签到见『正常账号』",
    emptyAction: { tab: "active", label: "查看正常账号" },
  },
  active: {
    key: "active",
    title: "正常账号",
    sub: "签到状态、排序与手动操作；删除后 7 天内可恢复。",
    searchLabel: "搜索正常账号",
    tbody: "accounts-tbody",
    empty: "accounts-empty",
    count: "accounts-active-count",
    bar: "batch-bar-active",
    cnt: "batch-count-active",
    all: "select-all-active",
    emptyText: "暂无账号，添加后即可开始自动签到",
    emptyAction: { tab: null, add: true, label: "添加账号" },
  },
  deleted: {
    key: "deleted",
    title: "待删除账号",
    sub: "软删除保留 7 天，期内可恢复；彻底删除需管理员口令。",
    searchLabel: "搜索待删除账号",
    tbody: "accounts-deleted-tbody",
    empty: "accounts-deleted-empty",
    count: "accounts-deleted-count",
    bar: "batch-bar-deleted",
    cnt: "batch-count-deleted",
    all: "select-all-deleted",
    emptyText: "暂无待删除账号",
    emptyAction: { tab: "active", label: "返回正常账号" },
  },
};

export function groupSpec(group) {
  return GROUPS[group];
}

/* ---------------- 分组 / 排序 ---------------- */
export function sortedPending(accounts) {
  // 待审核置顶（新提交在前）、已拒绝沉底；accounts 表无时间戳，id 与提交先后单调一致，
  // 以 index 作时间代理。显示顺序不影响批量操作（选中以手机号对齐，见 selectedAccounts）。
  return accounts
    .filter(function (a) {
      return (a.status === "pending" || a.status === "rejected") && !a.deleted;
    })
    .sort(function (a, b) {
      if (a.status === b.status) return b.index - a.index;
      return a.status === "pending" ? -1 : 1;
    });
}

export function groupAll(accounts, group) {
  if (group === "pending") return sortedPending(accounts);
  if (group === "active") {
    return accounts.filter(function (a) {
      return a.status === "active" && !a.deleted;
    });
  }
  return accounts.filter(function (a) {
    return a.deleted;
  });
}

/* ---------------- 检索 ---------------- */
// 列表已脱敏：输入完整号时同样 mask 后匹配，保证搜索可用（脱敏口径由调用方注入
// maskPhone——唯一实现在 core.js，本层不重写第二份）。
export function accountMatch(a, kw, maskPhone) {
  if (!kw) return true;
  var q = String(kw).toLowerCase();
  var masked = maskPhone ? maskPhone(q) : q;
  return [a.name, a.phone, a.owner_display, a.owner].some(function (v) {
    var s = String(v || "").toLowerCase();
    return s.indexOf(q) !== -1 || s.indexOf(masked) !== -1;
  });
}

export function filterGroup(accounts, group, kw, maskPhone) {
  return groupAll(accounts, group).filter(function (a) {
    return accountMatch(a, kw, maskPhone);
  });
}

/* ---------------- 选中集（身份键 = 手机号） ---------------- */
// 选中集以**手机号**为身份键：index 只是列表位置，move 改持久化顺序后同一 index 指向
// 另一个账号，按 index 记选中会让「上移」把勾选粘到邻居身上、批量操作误伤他人。
export function byPhone(accounts, phone) {
  return accounts.filter(function (a) { return a.phone === phone; })[0];
}

export function selectedAccounts(accounts, sel, group) {
  return Object.keys(sel[group] || {})
    .map(function (k) { return byPhone(accounts, k); })
    .filter(Boolean);
}

export function selectedIds(accounts, sel, group) {
  return selectedAccounts(accounts, sel, group).map(function (a) { return a.index; });
}

export function selectedPhones(accounts, sel, group) {
  return selectedAccounts(accounts, sel, group).map(function (a) { return a.phone; });
}

// 数据刷新后清掉已不存在的选中项（否则批量会带上已删除/已移出的账号）。
export function pruneSelection(accounts, sel) {
  var next = {};
  Object.keys(sel).forEach(function (g) {
    next[g] = {};
    Object.keys(sel[g] || {}).forEach(function (k) {
      if (byPhone(accounts, k)) next[g][k] = true;
    });
  });
  return next;
}

export function selectAllState(accounts, sel, group, kw, maskPhone) {
  var rows = filterGroup(accounts, group, kw, maskPhone);
  var picked = rows.filter(function (a) { return sel[group] && sel[group][a.phone]; }).length;
  return {
    checked: rows.length > 0 && picked === rows.length,
    indeterminate: picked > 0 && picked < rows.length,
  };
}

/* ---------------- 统计 / 状态 ---------------- */
export function statsOf(activeAccounts, states) {
  var success = 0;
  var failed = 0;
  var waiting = 0;
  var skipped = 0;
  activeAccounts.forEach(function (a) {
    var s = (states && states[a.phone]) || "pending";
    if (s === "success" || s === "already") success++;
    else if (s === "failed") failed++;
    else if (
      s === "no_task" || s === "skipped_window" || s === "skipped_norange" ||
      s === "paused" || s === "user_cancelled"
    ) skipped++;
    else waiting++;
  });
  return { success: success, failed: failed, waiting: waiting, skipped: skipped };
}

/* 状态词表收敛：文案 / 图标 / 语气档一律取自唯一事实源 lib/status-vocab.js，
   本页只保留到 CSS 类名的映射。
   账号表用 full（全称，信息量优先）；图标用规范化 icon。
   语气档再过一层 ACCT_TONE——账号页 CSS 只有 ok/bad/warn/muted 四档
   （app.css 的 .acct-state--*），语义档 info（如待签）在此收成 muted。 */
var ACCT_TONE = { ok: "ok", bad: "bad", warn: "warn", muted: "muted", info: "muted" };

export var STATE_ICON = pluckField(STATUS_VOCAB, "icon");
export var STATE_TEXT = pluckField(STATUS_VOCAB, "full");
export var STATE_TONE = pluckTone(STATUS_VOCAB);

function pluckField(vocab, field) {
  var out = {};
  for (var k in vocab) out[k] = vocab[k][field];
  return out;
}

function pluckTone(vocab) {
  var out = {};
  for (var k in vocab) out[k] = ACCT_TONE[vocab[k].tone] || "muted";
  return out;
}

/* 状态图例：由唯一事实源 STATUS_VOCAB 派生（每码 full + icon + 账号页语气档），
   保证账号表**能渲染的每个状态码**都有图例解释；不再手写 chip 表。
   合并口径：表格里同 symbol 同色的状态视觉上无从区分，故按「icon + ACCT_TONE 档」
   去重为一格、标签列出全部 full 名（顺序随 STATUS_VOCAB 插入序，稳定可测）。 */
export function legendEntries() {
  var order = [];
  var byGlyph = {};
  for (var code in STATUS_VOCAB) {
    var e = STATUS_VOCAB[code];
    var tone = ACCT_TONE[e.tone] || "muted";
    var glyph = e.icon + "|" + tone;
    if (!byGlyph[glyph]) {
      byGlyph[glyph] = { icon: e.icon, tone: tone, labels: [] };
      order.push(glyph);
    }
    byGlyph[glyph].labels.push(e.full);
  }
  return order.map(function (g) {
    var it = byGlyph[g];
    return { icon: it.icon, tone: it.tone, text: it.labels.join(" / ") };
  });
}

// 状态列：图标 + title（状态名 · 原因 · 耗时）。原因仅在不同于状态名时拼接，避免重复。
export function stateCell(phone, states, msgs, durs) {
  var code = (states && states[phone]) || "pending";
  var msg = (msgs && msgs[phone]) || "";
  var dur = durs && durs[phone];
  var base = STATE_TEXT[code] || "待签";
  var title = base + (msg && msg !== base ? " · " + msg : "")
    + (dur != null ? " · 耗时 " + Number(dur).toFixed(1) + "s" : "");
  return {
    code: code,
    text: base,
    title: title,
    icon: STATE_ICON[code] || "clock",
    tone: STATE_TONE[code] || "muted",
  };
}

export function badgeOf(status) {
  if (status === "pending") return { label: "待审核", tone: "warn" };
  if (status === "rejected") return { label: "已拒绝", tone: "bad" };
  return { label: "正常", tone: "ok" };
}

/* ---------------- 展示文案 ---------------- */
export function prefText(account) {
  if (!account.time_pref) return "—";
  var edge = account.time_pref_edge;
  if (edge === "first") return "最早 " + account.time_pref;
  if (edge === "last") return "最后 " + account.time_pref;
  return account.time_pref;
}

export function ownerText(account) {
  return account.owner_display || (account.owner === "admin" ? "管理员" : (account.owner || "—"));
}

// 「上次实领」= 最近一次有记录的业务日实际领取该账号的执行体（口径由后端定）。
// null = 最近一次有记录的业务日没有该账号的记录 → 显示「—」，不当成 unknown。
export function lastExecText(account) {
  var ex = account && account.last_executor;
  if (!ex) return "—";
  var label = String(ex.label || "");
  if (label) return label;
  return ex.role === "unknown" ? "未标注（旧数据）" : "—";
}

// 归属邮箱：列表态 a.owner 已由服务端脱敏，直接展示；非邮箱归属回落 ownerText。
export function ownerMailText(account) {
  var owner = String((account && account.owner) || "");
  return owner.indexOf("@") !== -1 ? owner : ownerText(account);
}

export function deletedAtText(account) {
  return String(account.deleted_at || "").replace("T", " ").slice(0, 16);
}

export function emptyText(group, all, filtered) {
  if (filtered.length) return "";
  if (all.length) return "无匹配结果";
  return GROUPS[group].emptyText;
}

export function countLabel(all, filtered, kw) {
  if (!all) return "";
  return kw ? filtered + " 个匹配 / 共 " + all + " 个" : all + " 个";
}

/* ---------------- 行菜单 / 批量动作 ---------------- */
// 行菜单以**数据**给出（{key,label,icon,danger}），组件按 key 派发给 ops；宽窄两版共用同一份。
export function menuItems(group) {
  if (group === "pending") {
    return [
      { key: "approve", label: "通过", icon: "check" },
      { key: "reject", label: "驳回", icon: "x" },
      { key: "edit", label: "编辑", icon: "pencil" },
      { key: "remove", label: "删除", icon: "trash", danger: true },
    ];
  }
  if (group === "deleted") {
    return [
      { key: "restore", label: "恢复", icon: "rotate-ccw" },
      { key: "purge", label: "彻底删除", icon: "trash", danger: true },
    ];
  }
  return [
    { key: "move_up", label: "上移", icon: "arrow-up" },
    { key: "move_down", label: "下移", icon: "arrow-down" },
    { key: "signin", label: "手动签到", icon: "play" },
    { key: "edit", label: "编辑", icon: "pencil" },
    { key: "remove", label: "删除", icon: "trash", danger: true },
  ];
}

export function menuItemsWithout(group, omitKeys) {
  var omit = omitKeys || [];
  return menuItems(group).filter(function (it) { return omit.indexOf(it.key) === -1; });
}

export function batchActions(group) {
  if (group === "pending") {
    return [
      { key: "approve", label: "通过", variant: "btn--primary" },
      { key: "reject", label: "驳回", variant: "btn--ghost" },
    ];
  }
  if (group === "deleted") {
    return [
      { key: "restore", label: "恢复", variant: "btn--primary" },
      { key: "purge", label: "彻底删除", variant: "btn--ghost btn--danger-ghost" },
    ];
  }
  return [
    { key: "signin", label: "手动签到", variant: "btn--primary" },
    { key: "delete", label: "删除", variant: "btn--ghost" },
  ];
}
