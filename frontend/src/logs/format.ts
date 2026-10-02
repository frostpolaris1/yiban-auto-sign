/**
 * 日志页纯逻辑（Vitest 单测）。
 *
 * 契约来源：`web/routes/data.py::api_logs` 的响应——logs/total_lines/returned/truncated/
 * dropped_lines/q/log_file/date/is_today/probe_events/sign_events/recent_*_date。
 * 三元组语义（服务端注释里点名的历史缺陷）：`truncated` = "结果集比本次返回的行数更大"，
 * 既含 all=1 的封顶截断，也含缺省尾部 80 行截断；**前端只按 truncated 判断是否有更多**，
 * 不得用行数自行推断（否则两侧口径必然漂移）。
 */

export interface LogEvent {
  time: string;
  phone: string;
  status: string;
  /** 仅签到事件有 */
  attempt?: number;
  message: string;
}

export interface LogsPayload {
  logs: string[];
  total_lines: number;
  returned: number;
  truncated: boolean;
  dropped_lines: number;
  q: string;
  log_file: string;
  date: string;
  is_today: boolean;
  probe_events: LogEvent[];
  sign_events: LogEvent[];
  recent_log_date: string;
  recent_probe_date: string;
  recent_sign_date: string;
}

export type TabKey = "main" | "signev" | "probe";
export const TAB_KEYS: TabKey[] = ["main", "signev", "probe"];

export const EVENT_PAGE_SIZES = [20, 50, 100];

/** 日志行信息栏文案。
 *  三分支与 legacy 一致：**检索态**报"X 行匹配 / 共 Y 行"（否则用户会以为总数被过滤前口径）；
 *  截断态报"显示 X / 共 Y 行"；否则只报总数（截断口径已由前一行覆盖）。
 *  检索态判据取**服务端回显**的 `q`（payload 自带），与页面已生效的过滤条件同源。 */
export function infoText(
  p: Pick<LogsPayload, "truncated" | "returned" | "total_lines" | "dropped_lines" | "q">,
): string {
  const base = p.q
    ? `${p.returned} 行匹配 / 共 ${p.total_lines} 行`
    : p.truncated
      ? `已截断：显示 ${p.returned} / 共 ${p.total_lines} 行`
      : `共 ${p.total_lines} 行`;
  return p.dropped_lines > 0 ? `${base}（另有 ${p.dropped_lines} 行未计入）` : base;
}

/* ---------------- 事件状态口径（终态码 → 中文标签 + 色调） ----------------
   legacy 用色调徽标 + `title=原始码` 呈现；未知码回落**原始码 + muted**（信息不丢）。
   探针只有 failed/其它 两态（非 failed 一律"正常"，与旧版 ✅/❌ 口径一致），
   `__default` 兜住将来新增的非失败状态。 */

export type StatusTone = "ok" | "bad" | "warn" | "info" | "muted";

export const SIGN_STATUS_MAP: Record<string, [string, StatusTone]> = {
  success: ["成功", "ok"],
  already: ["已签到", "ok"],
  no_task: ["无需签到", "muted"],
  failed: ["失败", "bad"],
  retrying: ["重试中", "warn"],
  pending: ["待签", "info"],
  skipped_window: ["超出时段", "warn"],
  skipped_norange: ["不在范围", "warn"],
  paused: ["已暂停", "muted"],
  user_cancelled: ["已取消", "muted"],
};

export const PROBE_STATUS_MAP: Record<string, [string, StatusTone]> = {
  failed: ["异常", "bad"],
  __default: ["正常", "ok"],
};

export interface StatusLabel {
  label: string;
  tone: StatusTone;
  /** 原始状态码（页面放在 title 上，信息不丢） */
  raw: string;
}

export function statusLabel(kind: "sign" | "probe", code: string): StatusLabel {
  const raw = String(code ?? "");
  if (kind === "probe") {
    const mapped = PROBE_STATUS_MAP[raw] ?? PROBE_STATUS_MAP.__default;
    return { label: mapped[0], tone: mapped[1], raw };
  }
  const mapped = SIGN_STATUS_MAP[raw];
  return mapped ? { label: mapped[0], tone: mapped[1], raw } : { label: raw || "未知", tone: "muted", raw };
}

/** 事件行消息：签到事件的尝试号只在**消息里没有它**时补（retrying 的消息由引擎带号）。 */
export function eventMessage(ev: LogEvent, isSign: boolean): string {
  let msg = String(ev.message || "");
  const attempt = Number(ev.attempt ?? 0);
  if (isSign && ev.status !== "retrying" && attempt > 1) {
    msg = msg ? `${msg}（第 ${attempt} 次）` : `（第 ${attempt} 次）`;
  }
  return msg;
}

export function eventCountText(n: number): string {
  return `共 ${n} 条`;
}

/** 查询串：date 必带；q 与 all=1 仅在生效时下发（避免无意义参数）。 */
export function buildLogsQuery(date: string, q: string, all: boolean): string {
  const params = new URLSearchParams();
  if (date) params.set("date", date);
  const kw = (q || "").trim();
  if (kw) params.set("q", kw);
  if (all) params.set("all", "1");
  const qs = params.toString();
  return `/api/logs${qs ? `?${qs}` : ""}`;
}

/** 导出下载地址（GET，无需 CSRF；走 <a download> 而非 fetch）。 */
export function buildExportUrl(date: string): string {
  return `/api/logs/export?date=${encodeURIComponent(date)}`;
}

/**
 * 是否允许本次自动轮询。条件与 legacy `pollTick` 逐条一致：
 * 页面可见 + 开关开 + **处于"跟随最新"视图**（用户 pin 住某一天就不轮询）+
 * 无在途请求 + 无打开的浮层（模态/下拉）。
 * 注意第三条件是"是否跟随"而非"是否今天"——用 isToday 判会与 legacy 漂移：
 * 用户用"查看该日"pin 住今天时，legacy 停止轮询，而 isToday 仍为真。
 */
export function shouldPoll(opts: {
  visibility: DocumentVisibilityState;
  autoRefresh: boolean;
  following: boolean;
  busy: boolean;
  overlayOpen: boolean;
}): boolean {
  return (
    opts.visibility === "visible" &&
    opts.autoRefresh &&
    opts.following &&
    !opts.busy &&
    !opts.overlayOpen
  );
}

/** 事件行的排序（客户端全量排序；time 是 HH:MM:SS 文本，可按字典序排）。 */
export function sortEvents(
  rows: LogEvent[],
  prop: "time" | "phone" | "status" | "attempt" | "message",
  order: "ascending" | "descending",
): LogEvent[] {
  const dir = order === "ascending" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const av = prop === "attempt" ? Number(a.attempt ?? 0) : String(a[prop] ?? "");
    const bv = prop === "attempt" ? Number(b.attempt ?? 0) : String(b[prop] ?? "");
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    return 0;
  });
}

export function paginate<T>(rows: T[], page: number, pageSize: number): T[] {
  const start = (Math.max(1, page) - 1) * pageSize;
  return rows.slice(start, start + pageSize);
}
