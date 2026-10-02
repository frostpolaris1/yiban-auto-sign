/**
 * 签到日历的口径类型（与 `model.js` 的纯 JS 实现配对）。
 *
 * 为什么类型单独一个文件：`model.js` 刻意保持纯 JS（Python 的对拍测试按字面量抽函数并
 * 在 Node 里真跑，见该文件头部说明），而 Vue 组件与单测仍要类型——于是类型只在这里声明，
 * 不作为运行时模块。**接口字段名与实现保持一致**：改字段必须同时改 `model.js` 与消费方。
 */

/** 服务端下发的显示档（唯一事实源 `yiban.status.DISPLAY`，经 display_payload 下发）。 */
export interface StateEntry {
  label: string;
  tone: Tone;
}

export interface CodeEntry {
  symbol: string;
  text: string;
  tone: Tone;
}

export type Tone = "ok" | "bad" | "muted" | "warn" | "busy";

export interface DayOff {
  reason: string;
  text: string;
  tone: Tone;
}

export interface LegendItem {
  tone: string;
  label: string;
}

export interface CalendarCtx {
  by_code: Record<string, CodeEntry>;
  by_symbol: Record<string, StateEntry>;
  /** 今天被急停/周末门挡下的原因（服务端读 .env 真值后下发）；照常为 null。 */
  day_off: DayOff | null;
  /** 图例条目（按语气档归组）；服务端由 `legend_items()` 注入。 */
  legend?: LegendItem[];
}

/** 日期格的输入（`dayCell` 的第一个参数）。 */
export interface CalendarCellInput {
  d: number;
  date: string;
  /** 该日状态符号（按日状态文件的存储口径）；无记录为空串。 */
  state: string;
  off: boolean;
  offDay: "" | "日" | "六";
  isToday: boolean;
  selected: boolean;
}

/** 日期格的输出：数据而非 HTML（标签与属性绑定属于 Vue 模板）。 */
export interface CalendarCell {
  d: number;
  date: string;
  cls: string;
  label: string;
  pressed: boolean;
  offBadge: "" | "日" | "六";
}

/** 周末签到开关（服务端 `weekend_flags` 的解析结果）。 */
export interface CalendarFlags {
  saturday: boolean;
  sunday: boolean;
}

/** `/api/my-calendar` 的响应。 */
export interface CalendarPayload {
  ok: boolean;
  month: string;
  days: Record<string, Record<string, string>>;
  sunday_sign: number;
  saturday_sign: number;
}

/** `/api/my-logs` 的响应。 */
export interface MyLogsPayload {
  ok: boolean;
  date: string;
  logs: string[];
}

/** 账号列表项（`/api/my-accounts`；只取本页用到的字段）。 */
export interface AccountItem {
  display_name: string;
  phone: string;
  phone_model?: string;
  status: string;
  deleted?: boolean;
  state_status?: string;
  state_message?: string;
  queue_ahead?: number;
}

export interface StatusLine {
  cls: string;
  text: string;
}
