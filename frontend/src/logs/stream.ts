/**
 * SSE 实时进度流的前端状态机（`/api/stream/progress`）。
 *
 * 契约来源：`web/routes/stream_api.py` 与 `yiban/store/run_events.py`（快照/事件的
 * 唯一投影点）。纪律：
 * · 标签由服务端下发（`node_labels` / `progress.node_label`），前端**不另抄节点表**；
 * · 事件计数**不去重**：字段标签写「成功事件」，绝不写「成功账号」——首页「今日成功账号」
 *   来自 `sign_events` 按账号去重，两套口径不得混算（125 §4.4 的防火墙）；
 * · `finalize` 单独一列，不并入任何「完成数」。
 */

export interface StreamTotals {
  claim: number;
  start: number;
  success: number;
  fail: number;
  pause: number;
  finalize: number;
}

export interface StreamExecRow {
  executor: string;
  executor_label: string;
  claim: number;
  start: number;
  success: number;
  fail: number;
  pause: number;
  finalize: number;
  unexecuted: number;
  first_ts: string;
  last_ts: string;
  duration_sec: number | null;
}

export interface StreamWindow {
  start_day: string;
  end_day: string;
  in_window: boolean;
  retention_days: number;
}

export interface StreamSnapshot {
  day: string;
  window: StreamWindow;
  totals: StreamTotals;
  by_executor: StreamExecRow[];
  /** 节点顺序与中文标签：服务端下发，前端只渲染 */
  node_order: string[];
  node_labels: Record<string, string>;
  cursor: number;
  generated_ts: string;
  truncated: boolean;
}

export interface StreamProgress {
  id: number;
  ts: string;
  day: string;
  node: string;
  node_label: string;
  executor: string;
  executor_label: string;
  message: string;
}

export interface StreamState {
  /** 快照业务日；未建连（无快照）时为空串 */
  day: string;
  /** 保留窗口；未建连时为 null（页面据此显示占位，不自算 14 天） */
  window: StreamWindow | null;
  /** 节点顺序与中文标签：由服务端下发，前端只渲染，**不另抄节点表** */
  nodeOrder: string[];
  nodeLabels: Record<string, string>;
  totals: StreamTotals;
  execs: StreamExecRow[];
  recent: StreamProgress[];
  lastEventAt: string;
}

/** 最近事件带最多留几条（一屏可读，不无限增长）。 */
export const RECENT_MAX = 8;

function zeroTotals(): StreamTotals {
  return { claim: 0, start: 0, success: 0, fail: 0, pause: 0, finalize: 0 };
}

export function emptyStreamState(): StreamState {
  return {
    day: "",
    window: null,
    nodeOrder: [],
    nodeLabels: {},
    totals: zeroTotals(),
    execs: [],
    recent: [],
    lastEventAt: "",
  };
}

/** 快照 → 状态（建连/重同步时整体替换）。 */
export function stateFromSnapshot(snap: StreamSnapshot): StreamState {
  const totals = zeroTotals();
  for (const k of Object.keys(totals) as (keyof StreamTotals)[]) {
    totals[k] = Number(snap.totals?.[k] ?? 0);
  }
  return {
    day: String(snap.day ?? ""),
    window: snap.window ?? null,
    nodeOrder: [...(snap.node_order ?? [])],
    nodeLabels: { ...(snap.node_labels ?? {}) },
    totals,
    execs: (snap.by_executor ?? []).map((r) => ({ ...r })),
    recent: [],
    lastEventAt: "",
  };
}

/**
 * 增量进度事件 → 新状态（不可变：返回新对象，供 Vue 响应式识别）。
 *
 * 计数按事件行累加（不去重）；执行体行不存在时按需新建。
 */
export function applyProgress(state: StreamState, ev: StreamProgress): StreamState {
  const totals = { ...state.totals };
  const key = ev.node as keyof StreamTotals;
  if (key in totals) totals[key] = Number(totals[key] ?? 0) + 1;

  const execs = state.execs.map((r) => ({ ...r }));
  let row = execs.find((r) => r.executor === ev.executor);
  if (!row) {
    row = {
      executor: ev.executor,
      executor_label: ev.executor_label,
      claim: 0,
      start: 0,
      success: 0,
      fail: 0,
      pause: 0,
      finalize: 0,
      unexecuted: 0,
      first_ts: ev.ts,
      last_ts: ev.ts,
      duration_sec: null,
    };
    execs.push(row);
  }
  if (key in row) (row as unknown as Record<string, number>)[key] += 1;
  if (ev.ts && (!row.last_ts || ev.ts > row.last_ts)) row.last_ts = ev.ts;
  if (ev.ts && (!row.first_ts || ev.ts < row.first_ts)) row.first_ts = ev.ts;
  // 按最近活动降序（与快照一致），让活跃执行体始终在首行
  execs.sort((a, b) => (a.last_ts < b.last_ts ? 1 : a.last_ts > b.last_ts ? -1 : 0));

  const recent = [ev, ...state.recent].slice(0, RECENT_MAX);
  return { ...state, totals, execs, recent, lastEventAt: ev.ts || state.lastEventAt };
}

/** 连接状态文案（`● 流已连接` / `○ 已断开，正在重连` / `— 已关闭(原因)`）。
 *
 * `reason` 走 `endReasonText` 翻成中文（`end` 事件回的是机器可读原因串）。
 */
export function streamStatusText(state: "connecting" | "open" | "closed", reason = ""): string {
  if (state === "open") return "● 流已连接";
  if (state === "closed") return reason ? `— 已关闭（${endReasonText(reason)}）` : "— 已关闭";
  return "○ 已断开，正在重连";
}

/** `end` 事件里"不再自动重连"的原因集合。
 *
 * 其余原因（`idle_timeout` / `max_duration` / `window_closed`）是服务端主动轮换长连接，
 * 浏览器 `EventSource` 应自动重连；集合内的原因重连只会再被拒，故前端主动 `close()` 并降级。
 */
export const TERMINAL_END_REASONS: ReadonlySet<string> = new Set([
  "disabled",
  "session_expired",
  "too_many_connections",
  "server_busy",
]);

/** 该 `end` 原因是否终止重连（是则前端降级到既有轮询）。 */
export function isTerminalEnd(reason: string): boolean {
  return TERMINAL_END_REASONS.has(reason);
}

/** 快照窗口 → 一行说明（业务日 + 保留窗口 + 窗口内外）；无快照时回空串。 */
export function windowLine(state: StreamState): string {
  const w = state.window;
  if (!w) return "";
  const scope = w.in_window ? "窗口内" : "今天不在保留窗口内";
  return `业务日 ${state.day} · 保留窗口 ${w.start_day} 至 ${w.end_day} · ${scope}`;
}

/** `end` 事件的原因 → 中文（前端不再自动重连时展示）。 */
export function endReasonText(reason: string): string {
  const map: Record<string, string> = {
    disabled: "服务端已关闭进度流",
    too_many_connections: "连接数超限",
    server_busy: "服务端繁忙",
    session_expired: "登录已过期，请重新登录",
    idle_timeout: "长时间无进度事件",
    max_duration: "连接已达时限，正在重连",
    window_closed: "签到窗口已关闭",
  };
  return map[reason] ?? reason ?? "";
}

/** 最近事件带文案：节点 + 执行体标签，**不含账号**。 */
export function recentText(recent: StreamProgress[]): string {
  if (!recent.length) return "—";
  return recent
    .map((e) => {
      const hm = String(e.ts ?? "").match(/\d{2}:\d{2}:\d{2}/);
      return `${hm ? hm[0] : e.ts} ${e.executor ?? ""} ${e.node_label ?? e.node}`;
    })
    .join(" / ");
}

/** 每执行体一行的存活/让位角标（**派生自计数**，不新造事件；存活口径仍走既有端点）。 */
export function execRowNote(row: StreamExecRow): string {
  if (row.executor === "fallback" && row.claim === 0) return "已让位/静默";
  if (row.claim > 0) return "有活动";
  return "待命";
}
