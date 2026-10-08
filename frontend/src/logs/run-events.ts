/**
 * 巡检面契约（Vitest 单测）：`GET /api/admin/run-events` 的消费侧。
 *
 * 契约来源：`web/routes/run_events_api.py` 的响应与 `yiban/store/run_events.py` 的读取面。
 * 纪律：
 * · 脱敏单出口在服务端（账号已遮、执行体只回角色与槽位），前端只插值渲染、零自遮；
 * · 节点中文标签由服务端下发（`node_label`），前端不另抄一张节点表；
 * · 保留期可见窗口由服务端回显（`window`），前端只渲染，不自算 14 天。
 */

export interface RunRound {
  day: string;
  /** 公开执行体标签（角色 + 槽位序号，不含主机名） */
  executor: string;
  executor_label: string;
  claim: number;
  start: number;
  success: number;
  fail: number;
  pause: number;
  /** 领取后未发起请求的账号数（有 claim 无 start） */
  unexecuted: number;
  /** 该轮耗时（秒）；时刻解析不出时为 null */
  duration_sec: number | null;
  first_ts: string;
  last_ts: string;
}

export interface RunEvent {
  ts: string;
  node: string;
  node_label: string;
  /** 已遮罩的账号形态 */
  phone: string;
  message: string;
}

export interface RunEventsWindow {
  /** 保留窗口起点（含） */
  start_day: string;
  /** 保留窗口终点（含，通常今天） */
  end_day: string;
  /** 本次响应对应的业务日 */
  day: string;
  /** day 是否落在保留窗口内 */
  in_window: boolean;
  /** 表内真实最早/最晚业务日（无数据时 null） */
  min_day: string | null;
  max_day: string | null;
  has_data: boolean;
}

export interface RunEventsPayload {
  ok: boolean;
  retention_days: number;
  window: RunEventsWindow;
  rounds: RunRound[];
  /** 轮级摘要行被 `MAX_ROUNDS` 截断（本日轮数超过上限）；页面必须写明 */
  rounds_truncated: boolean;
  /** 读取层的轮级上限（用于写明"本日最多 N 轮"） */
  rounds_limit: number;
  events: RunEvent[];
  events_truncated: boolean;
  events_limit: number;
}

/** 轮次定位键（同一业务日同一执行体即同一轮）。 */
export function roundKey(r: Pick<RunRound, "day" | "executor">): string {
  return `${r.day}|${r.executor}`;
}

/** 轮级摘要取值：从多行里取回选中那一轮；找不到回 null。 */
export function findRound(rounds: RunRound[], key: string): RunRound | null {
  for (const r of rounds) {
    if (roundKey(r) === key) return r;
  }
  return null;
}

/**
 * 本次响应后应当选中的轮次键。
 *
 * 两种意图分开，这是本函数的唯一理由：
 * · 导航态（`keepSelection=false`）：用户显式指定执行体就选它，否则选该日最新一轮。
 *   日期切换、「查看该日」走这条。
 * · 保留态（`keepSelection=true`）：**在途选择优先**——当前选中仍在新数据里就留。
 *   10 秒轮询与刷新走这条。为什么必须保留：轮询无条件回落最新一轮，会把用户点开的
 *   旧轮在 ≤10 秒内改回最新，主用途「查昨天那轮」因此失效。
 *   当前选中不在新数据里（例如跨天或被清理）才回落该日最新一轮。
 */
export function nextSelectedKey(
  rounds: RunRound[],
  day: string,
  requestedExecutor: string,
  currentKey: string,
  keepSelection: boolean,
): string {
  if (keepSelection) {
    if (currentKey && findRound(rounds, currentKey)) return currentKey;
    return rounds.length ? roundKey(rounds[0]) : "";
  }
  if (requestedExecutor) return `${day}|${requestedExecutor}`;
  return rounds.length ? roundKey(rounds[0]) : "";
}

/** 轮级摘要截断文案：未截断回空串；截断时写明本日上限（不许静默丢轮）。 */
export function roundsTruncatedText(
  p: Pick<RunEventsPayload, "rounds_truncated" | "rounds_limit">,
): string {
  if (!p.rounds_truncated) return "";
  return `轮级摘要已截断（本日最多 ${p.rounds_limit} 轮），更早的轮次未列出。`;
}

/** 巡检查询串：日期缺省时不带 `day`（服务端取表内最新业务日）。 */
export function buildRunEventsQuery(day: string, executor: string): string {
  const params = new URLSearchParams();
  if (day) params.set("day", day);
  if (executor) params.set("executor", executor);
  const qs = params.toString();
  return `/api/admin/run-events${qs ? `?${qs}` : ""}`;
}

/**
 * 本次响应是否需要"去掉 executor 重取一次"（R2：选中态与时间线必须同源）。
 *
 * 带 `executor` 的请求里，服务端总会回该执行体的时间线；但 `rounds` 可能不含该执行体
 * 的分组（该执行体当日无行，或该日分组数超过读取层上限而被截断）。届时前端把
 * `selectedKey` 设成该执行体，`findRound` 却在其外——标题取该日最新轮、时间线却是该
 * 执行体的，两块不同源。本函数判定这种情形，调用方据此不带 executor 重取一次。
 * `executor` 为空时不重取：缺省口径本来就是"服务端取最新一轮"，天然同源。
 */
export function needsExecutorFallback(
  rounds: RunRound[],
  day: string,
  executor: string,
): boolean {
  if (!executor) return false;
  return findRound(rounds, roundKey({ day, executor })) === null;
}

/** 耗时文案：null → "--"；小于 60 秒给"N 秒"，否则给"N 分 M 秒"。 */
export function formatDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return "--";
  const n = Math.max(0, Math.round(sec));
  if (n < 60) return `${n} 秒`;
  const m = Math.floor(n / 60);
  const s = n % 60;
  return s === 0 ? `${m} 分` : `${m} 分 ${s} 秒`;
}

/** 事件时刻的时分秒（服务端下发 "YYYY-MM-DD HH:MM:SS"；形状不符时原样回）。 */
export function eventTime(ts: string): string {
  const text = String(ts ?? "");
  const m = text.match(/\d{2}:\d{2}:\d{2}/);
  return m ? m[0] : text || "--:--:--";
}

/** 可见窗口文案（页面必须显式写明，否则"空表"会被读成"那几天没数据"）。 */
export function windowText(win: RunEventsWindow, retentionDays: number): string {
  const have = win.min_day && win.max_day ? `库内实际数据 ${win.min_day} ~ ${win.max_day}` : "库内暂无数据";
  return `进度事件只保留最近 ${retentionDays} 天（${win.start_day} ~ ${win.end_day}）；${have}`;
}

/** 窗口外指引：落在保留期外时给"跨月回溯请走审计日志页"，否则回空串。 */
export function outOfWindowHint(win: RunEventsWindow): string {
  if (win.in_window) return "";
  return `该日期已超出 ${win.start_day} ~ ${win.end_day} 的保留窗口；跨月回溯请走「审计日志」页。`;
}

/** 空态文案（区分"窗口外"与"窗口内该日无运行记录"，两者都不许静默出空表）。 */
export function emptyRoundsText(win: RunEventsWindow): string {
  const out = outOfWindowHint(win);
  if (out) return out;
  return `${win.day} 没有运行进度记录（该日在保留窗口内）。`;
}
