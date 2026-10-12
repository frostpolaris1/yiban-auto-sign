/**
 * 自选签到时段（5 分钟粒度）的展示口径（纯函数，Vitest 单测）。
 *
 * **语义逐条对齐 legacy `web/static/js/components/time-pref.js`**：
 * 四态（不可选 / 常规 / 满员 / 部分裁剪）+ 选中态叠加、百分比文案、首尾时段提醒、
 * 预计时段说明、默认折叠规则、加载失败的原地错误态。
 *
 * 数据形状来自 `GET /api/my-time-pref`（`slots[]` 由后端算好：`disabled` = 完全落入
 * 掐头去尾裁剪区；`pct` = 该时段的已选热度；`edge_note` = 部分裁剪的说明）。
 */

export interface TimeSlot {
  slot_min: number;
  label: string;
  /** 完全落入裁剪区：不可选（后端算，前端不重算窗口） */
  disabled?: boolean;
  /** 已选热度百分比；>=100 为满员（**仍可选**：先到先得 + 溢出顺延） */
  pct: number;
  /** 部分落入裁剪区时的说明（有值即"虚线框"态，调度在可用部分执行） */
  edge_note?: string;
}

export interface TimePrefData {
  has_account: boolean;
  window: string;
  slots: TimeSlot[];
  pref_slot: number | null;
  allowed: boolean;
  pref?: boolean;
  estimated?: string;
  estimate_note?: string;
}

export const SLOT_OFF_NOTE = "该时段被掐头去尾保留，不可选择";
export const SLOT_FULL_PCT = 100;
export const DISABLED_HINT = "功能未开启：你的选择会保存，但暂不生效";
export const LOAD_ERROR_TEXT = "签到时间加载失败，请重试";
export const FIRST_SLOT_TIP = "最早时段：窗口开始后优先为你签到";
export const LAST_SLOT_TIP = "最后时段：临近窗口截止执行，网络波动可能导致错过";
export const NOT_ENABLED_PREFIX = "未开启：";

/** 槽位样式类（顺序即优先级：不可选 > 选中 > 满员 > 部分裁剪）。 */
export function slotClass(slot: TimeSlot, prefSlot: number | null): string {
  if (slot.disabled) return "slot--off";
  if (prefSlot === slot.slot_min) return "slot--on";
  if (slot.pct >= SLOT_FULL_PCT) return "slot--full";
  if (slot.edge_note) return "slot--partial";
  return "";
}

/** 槽位第二行文案：不可选报"已保留"，其余报热度。 */
export function slotPctText(slot: TimeSlot): string {
  return slot.disabled ? "已保留" : `已选 ${slot.pct}%`;
}

/** 槽位 title：不可选说明；部分裁剪说明；其余为空（不设 title）。 */
export function slotTitle(slot: TimeSlot): string {
  if (slot.disabled) return SLOT_OFF_NOTE;
  if (slot.edge_note) return `${slot.edge_note}，选中后将在可用部分为你签到`;
  return "";
}

/**
 * 首尾时段提醒：**仅当选中的槽位是首/尾**时给提示（提醒语义只对边界时段成立）。
 * 部分裁剪说明优先于首尾文案；`allowed` 为假时前缀"未开启："（选择会保存但不生效）。
 */
export function edgeTip(data: TimePrefData, index: number, slotCount: number): string {
  const slot = data.slots[index];
  if (!slot || data.pref_slot !== slot.slot_min) return "";
  const isEdge = index === 0 || index === slotCount - 1;
  if (!isEdge) return "";
  const base = slot.edge_note
    ? `${slot.edge_note}，选中后将在可用部分为你签到`
    : index === 0
      ? FIRST_SLOT_TIP
      : LAST_SLOT_TIP;
  return (data.allowed ? "" : NOT_ENABLED_PREFIX) + base;
}

/**
 * 预计时段行：已有生效自选（allowed && pref）时不显示（选择本身即答案）；
 * 否则优先显示后端给的 estimated（带说明与"未开启"后缀），退化为仅说明。
 */
export function estimateText(data: TimePrefData): { text: string; hidden: boolean } {
  if (data.allowed && data.pref) return { text: "", hidden: true };
  if (data.estimated) {
    return {
      text:
        `预计签到时段：${data.estimated}${data.estimate_note || ""}` +
        (data.allowed ? "" : "（自选未开启，按自动分配）"),
      hidden: false,
    };
  }
  const note = data.estimate_note || "";
  return { text: note, hidden: !note };
}

/** 默认折叠：未开启时收起（避免让用户以为可自选）；局部刷新时由调用方 preserve。 */
export function collapsedByDefault(allowed: boolean): boolean {
  return !allowed;
}

export function collapseLabel(collapsed: boolean): string {
  return collapsed ? "展开配置" : "收起";
}
