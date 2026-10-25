/**
 * 工作队列页（lcpx 试点，管理端 /work/queue）口径层——纯函数，无 DOM / 无网络 / 无全局。
 *
 * 数据面（契约见 docs/dev/api-contract.md §2.11 与 web/routes/queue_api.py）：
 *   · 待审核组 = 既有 `GET /api/accounts?group=pending`（服务端过滤分页，无缺口）；
 *   · 今日失败 / 今日未签 = 新只读聚合 `GET /api/queue?bucket=…`（按当日终态过滤，
 *     终态唯一事实源在服务端状态文件，前端零聚合）；
 *   · 今日摘要（业务日/窗口/计数）随 `/api/queue` 响应下发，前端不组装第二个口径。
 *
 * 脱敏纪律：两种服务端行的 phone / owner 都是**已遮值**；本层透传渲染，
 * 不做任何遮罩加工（遮罩唯一实现在 yiban.masking）。重试动作需要完整手机号，
 * 它只在「详情接口响应 → 登录请求体」之间内存流转，绝不进 URL。
 *
 * 抽屉两形态：≤720px 全屏（size 100%）、宽屏侧滑 480px（覆盖式，不拉宽内容列）。
 * 初始焦点落抽屉关闭钮——破坏面与 ep-confirm「焦点落取消侧」同一安全口径。
 */
import { STATUS_VOCAB } from "../lib/status-vocab.js";

export type GroupKey = "pending" | "failed" | "unsigned";
export type QueueBucket = Exclude<GroupKey, "pending">;

/** 服务端行的并集形状（/api/accounts pending 组行 ∪ /api/queue 行）。字段皆可缺。 */
export interface QueueRow {
  index: number;
  name?: string;
  phone?: string;
  phone_model?: string;
  display_name?: string;
  owner?: string;
  owner_display?: string;
  status?: string;
  reject_reason?: string;
  state?: string;
  state_msg?: string;
  state_time?: string;
  plan_at?: string | null;
  time_pref?: string | null;
}

/** 统一视图行：渲染层唯一消费形状（不携带服务端原始大对象）。 */
export interface RowView {
  index: number;
  name: string;
  phone: string;
  model: string;
  owner: string;
  status: string;
  rejectReason: string;
  state: string;
  stateText: string;
  stateMsg: string;
  planAt: string;
  timePref: string;
  group: GroupKey;
  badgeText: string;
}

export interface OpRequest {
  method: string;
  path: string;
  body?: unknown;
}

export interface QueueCounts {
  pending: number;
  rejected: number;
  active: number;
  failed: number;
  unsigned: number;
}

/** `/api/queue` 响应里的摘要字段（含计数）。 */
export interface QueueSummaryData {
  today: string;
  weekday: string;
  window: string;
  counts: QueueCounts;
}

/** `GET /api/queue` 响应形状（契约见 docs/dev/api-contract.md §2.11）。 */
export interface QueueListResponse {
  ok?: boolean;
  bucket?: string;
  today: string;
  weekday: string;
  window: string;
  counts: QueueCounts;
  rows: QueueRow[];
  total?: number;
}

/* ---------------- 三组待办的查询构造 ---------------- */

export function pendingQuery(limit: number, offset: number): string {
  return `/api/accounts?group=pending&limit=${limit}&offset=${offset}`;
}

export function queueQuery(bucket: QueueBucket, limit: number, offset: number): string {
  return `/api/queue?bucket=${bucket}&limit=${limit}&offset=${offset}`;
}

export function eventsQuery(index: number, days = 30): string {
  return `/api/queue/${index}/events?days=${days}`;
}

/* ---------------- 行归一 ---------------- */

/** 状态码 → 词表短名（唯一事实源 lib/status-vocab.js）；表外码返回空串（由调用方兜底）。 */
export function statusShort(code: string): string {
  const e = STATUS_VOCAB[code];
  return e ? e.short : "";
}

/** 事件行状态码 → 词表全称；表外码回落**原文**（不静默并进已知档，与日志页同口径）。 */
export function eventStatusText(code: string): string {
  const e = STATUS_VOCAB[code];
  return e ? e.full : String(code || "");
}

export function rowView(raw: QueueRow, group: GroupKey): RowView {
  const state = String(raw.state || "");
  const stateText = group === "pending" ? "" : statusShort(state) || "待签";
  const badgeText = group === "pending"
    ? (raw.status === "rejected" ? "已拒绝" : "待审核")
    : stateText;
  return {
    index: Number(raw.index),
    name: String(raw.display_name || raw.name || ""),
    phone: String(raw.phone || ""),
    model: String(raw.phone_model || ""),
    owner: String(raw.owner_display || raw.owner || ""),
    status: String(raw.status || ""),
    rejectReason: String(raw.reject_reason || ""),
    state,
    stateText,
    stateMsg: String(raw.state_msg || ""),
    planAt: String(raw.plan_at || ""),
    timePref: String(raw.time_pref || ""),
    group,
    badgeText,
  };
}

/* ---------------- 今日摘要 ---------------- */

export interface SummaryView {
  dateText: string;
  windowText: string;
  accountCount: number;
  failedCount: number;
  pendingCount: number;
}

export function summaryView(d: QueueSummaryData): SummaryView {
  return {
    dateText: `${d.today} ${d.weekday}`,
    windowText: `窗口 ${d.window}`,
    accountCount: Number(d.counts.active) || 0,
    failedCount: Number(d.counts.failed) || 0,
    pendingCount: (Number(d.counts.pending) || 0) + (Number(d.counts.rejected) || 0),
  };
}

/* ---------------- 行内操作（既有 ops 端点） ---------------- */

/**
 * 审核动作。`reason` 只在 reject 时下发；理由清洗由后端 `_nl_safe` 唯一负责，
 * 前端不复制第二份清洗规则（prompt 的 maxlength=100 与后端截断同宽）。
 */
export function reviewRequest(row: Pick<RowView, "index" | "phone">, action: "approve" | "reject", reason?: string): OpRequest {
  const body: Record<string, unknown> = { action, phone: row.phone };
  if (action === "reject") body.reason = reason ?? "";
  return { method: "POST", path: `/api/accounts/${row.index}/review`, body };
}

export function deleteRequest(row: Pick<RowView, "index" | "phone">): OpRequest {
  return { method: "DELETE", path: `/api/accounts/${row.index}`, body: { phone: row.phone } };
}

export interface AccountDetail {
  account?: { phone?: string };
}

/**
 * 重试（手动签到）请求：完整手机号必须来自**详情接口**且只在请求体里流转。
 * 详情缺失 / 号码非完整形态时返回 null（拒绝发请求，而不是把脱敏串发出去）。
 */
export function retryRequest(_row: RowView, detail?: AccountDetail): OpRequest | null {
  const phone = String(detail?.account?.phone || "");
  if (!/^\d{11}$/.test(phone)) return null;
  return { method: "POST", path: "/api/signin", body: { phone } };
}

/* ---------------- 抽屉状态机与两形态 ---------------- */

export interface DrawerState {
  open: boolean;
  target: RowView | null;
}

export function openDrawer(_s: DrawerState, row: RowView): DrawerState {
  return { open: true, target: row };
}

/** 关闭后目标保留（供退出过渡），`open` 决定是否渲染。 */
export function closeDrawer(s: DrawerState): DrawerState {
  return { open: false, target: s.target };
}

/** 抽屉初始焦点目标：关闭钮（安全侧，同 ep-confirm 焦点落取消侧的口径）。 */
export const FOCUS_TARGET = ".el-drawer__close-btn";

/** 宽屏下限：>720px 侧滑 480px，≤720px 全屏（与全站窄屏段同口径）。 */
export const WIDE_MIN = 721;

export interface DrawerForm {
  size: "100%" | "480px";
  cls: "obj-drawer--full" | "obj-drawer--side";
}

export function drawerForm(width: number): DrawerForm {
  return width <= WIDE_MIN - 1
    ? { size: "100%", cls: "obj-drawer--full" }
    : { size: "480px", cls: "obj-drawer--side" };
}
