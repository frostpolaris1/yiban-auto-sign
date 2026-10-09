/**
 * 「我的账号」页展示口径（纯函数，Vitest 单测）。
 *
 * **语义逐条对齐 legacy `web/static/js/components/my-accounts.js`**（P2 迁移期必须保真）：
 * 状态图标 / 状态徽标 / 无障碍文案 / 今日状态行 / 动作矩阵 / 各类提示文案。
 * 这里只放"算出来的东西"，不放 DOM——Vue 页面负责渲染，本模块负责口径。
 *
 * 注意一处**不可改写**的字面量：`todayStateText()` 的「今日已完成签到」是
 * `yiban.status.DISPLAY` 成功态文案之外的第二处副本，已被
 * `tests/test_yiban_status_single_source.py` 登记为"同一事实多份定义"总账的一项；
 * 改文案时两边同改，否则测试红。
 */

export type AccountStatus = "pending" | "rejected" | "active" | (string & {});

export interface MyAccount {
  display_name: string;
  phone: string;
  phone_model?: string;
  status: AccountStatus;
  user_paused?: boolean;
  deleted?: boolean;
  deleted_by_me?: boolean;
  /** 管理员直属账号为 true（后端下发，不写死角色判断） */
  pause_forbidden?: boolean;
  reject_reason?: string;
  logs?: string[];
  state_status?: string;
  queue_ahead?: number | null;
}

/** 审核状态 → 图标名（白名单；缺失回落 clock）。deleted 另有表达。 */
export const AUDIT_ICON: Record<string, string> = {
  pending: "clock",
  rejected: "circle-x",
  active: "circle-check",
};

export type BadgeTone = "" | "warn" | "bad" | "ok";

export function accountIcon(a: Pick<MyAccount, "deleted" | "status">): string {
  if (a.deleted) return "trash";
  return AUDIT_ICON[a.status] || "clock";
}

/** 标题行徽标：删除态优先，其次"用户主动暂停"，最后才是审核状态。 */
export function statusBadge(a: Pick<MyAccount, "deleted" | "user_paused" | "status">): {
  text: string;
  tone: BadgeTone;
} {
  if (a.deleted) return { text: "已删除", tone: "" };
  if (a.user_paused) return { text: "已取消", tone: "bad" };
  if (a.status === "pending") return { text: "待审核", tone: "warn" };
  if (a.status === "rejected") return { text: "已拒绝", tone: "bad" };
  if (a.status === "active") return { text: "已生效", tone: "ok" };
  return { text: a.status == null ? "" : String(a.status), tone: "" };
}

/** 读屏用状态文案（图标是纯装饰，语义必须另有文本出口）。 */
export function auditAriaText(a: MyAccount): string {
  if (a.deleted) {
    return a.deleted_by_me ? "状态：已删除（7 天内可撤销）" : "状态：已被管理员删除";
  }
  if (a.user_paused) return "状态：已取消（可恢复签到）";
  const map: Record<string, string> = { pending: "待审核", rejected: "已拒绝", active: "已生效" };
  return "状态：" + (map[a.status] || "未知");
}

/** 手机号 + 机型（机型可缺）。 */
export function accountMeta(a: Pick<MyAccount, "phone" | "phone_model">): string {
  return String(a.phone || "") + (a.phone_model ? " · " + a.phone_model : "");
}

/**
 * 今日状态行（**仅管理端**要求；用户端由签到日历页承担）。
 * 生效账号且未被用户暂停时才出现：已完成 → 固定文案；否则有排队数就报排队数。
 */
export function todayStateText(
  a: Pick<MyAccount, "deleted" | "status" | "state_status" | "queue_ahead">,
): string {
  if (a.deleted || a.status !== "active" || a.state_status === "paused") return "";
  const done = a.state_status === "success" || a.state_status === "already";
  if (done) return "今日已完成签到";
  if (a.queue_ahead != null) return `前方排队 ${a.queue_ahead} 人`;
  return "";
}

/** 删除态说明行。 */
export function deletedNote(a: Pick<MyAccount, "deleted" | "deleted_by_me">): string {
  if (!a.deleted) return "";
  return a.deleted_by_me
    ? "你已删除此账号，7 天内可撤销恢复，超期自动清除"
    : "已被管理员删除，待管理员处理";
}

/** 被拒账号的提示条文案（reason 为空则不编造原因）。 */
export function rejectedAlertText(a: Pick<MyAccount, "status" | "reject_reason">): string {
  if (a.status !== "rejected") return "";
  return "账号已被拒绝" + (a.reject_reason ? "：" + a.reject_reason : "") + "。修改后点「修改并重新提交」。";
}

/** 凭据异常（后端把签到置为 paused）时的提示条；生效账号之外不显示。 */
export function passwordAlertVisible(a: Pick<MyAccount, "deleted" | "status" | "state_status">): boolean {
  return !a.deleted && a.status === "active" && a.state_status === "paused";
}

export function passwordAlertText(): string {
  return "账号密码异常，签到已暂停，请编辑账号更新密码。";
}

export function pendingHintVisible(a: Pick<MyAccount, "deleted" | "status">): boolean {
  return !a.deleted && a.status === "pending";
}

export function pendingHintText(): string {
  return "审核通过后自动签到，结果见「签到日历」。";
}

export type AccountAction =
  | { kind: "calendar"; label: string }
  | { kind: "pause"; label: string }
  | { kind: "edit"; label: string }
  | { kind: "delete"; label: string }
  | { kind: "restore"; label: string }
  | { kind: "note"; text: string };

/**
 * 行内动作矩阵（顺序即渲染顺序，与 legacy 一致）：
 *   删除态：本人删的给「撤销删除」；管理员删的只提示"待管理员处理"（无动作）。
 *   生效态：日历链接（有 href 且非 inline 模式）→ 暂停/恢复（`pause_forbidden` 时不出现）
 *           → 编辑 → 删除。
 *   其余态（待审核/已拒绝）：编辑 → 删除；被拒的编辑按钮文案换成「修改并重新提交」。
 */
export function accountActions(
  a: MyAccount,
  opts: { calendarHref?: string | null; inline?: boolean } = {},
): AccountAction[] {
  if (a.deleted) {
    return a.deleted_by_me
      ? [{ kind: "restore", label: "撤销删除" }]
      : [{ kind: "note", text: "待管理员处理" }];
  }
  const out: AccountAction[] = [];
  if (a.status === "active") {
    if (!opts.inline && opts.calendarHref) out.push({ kind: "calendar", label: "签到日历" });
    if (!a.pause_forbidden) {
      out.push({ kind: "pause", label: a.user_paused ? "恢复签到" : "暂停签到" });
    }
  }
  out.push({ kind: "edit", label: a.status === "rejected" ? "修改并重新提交" : "编辑" });
  out.push({ kind: "delete", label: "删除" });
  return out;
}

/** 提交入口显隐：只要还有未删除账号就隐藏（全部被删时保留——软删除不死路）。 */
export function submitButtonVisible(accounts: MyAccount[]): boolean {
  return !accounts.some((a) => !a.deleted);
}
