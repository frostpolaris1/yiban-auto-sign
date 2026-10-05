/**
 * 在线校验任务的文案与节奏（纯函数，Vitest 单测）。
 *
 * **语义逐条对齐 legacy `web/static/js/components/verify-job.js`**。该组件的核心是
 * "诚实性"三条约定，迁移时最容易走形，故这里把文案与节奏单独固化并用测试钉住：
 *   1. 轮询有上界（`VERIFY_MAX_POLLS`），到顶要说"仍在进行、可稍后刷新"，不能无限转圈；
 *   2. 任何一次查询失败都**如实说失败并停止**，不把"查不到"说成"已结束"；
 *   3. 账号在提交那一刻已落库，校验结果只影响审核判断 ⇒ 全程不阻断提交、不回滚账号。
 *
 * 状态取值与后端 `yiban/store/verify_jobs.py` 一一对应，**不得自造**。
 */

/** 退避序列：前几次密（通常几秒出结果），随后拉长避免长期占请求。 */
export const VERIFY_POLL_MS = [1200, 2000, 3000, 5000, 8000, 10000];
/** 轮询上界（约 2~3 分钟） */
export const VERIFY_MAX_POLLS = 24;

const TERMINAL = new Set(["done", "rejected", "cancelled"]);

export function isTerminal(status: string): boolean {
  return TERMINAL.has(status);
}

/** 第 tries 次轮询后的等待时长（tries 从 1 起；超出序列长度取末项）。 */
export function pollDelay(tries: number): number {
  return VERIFY_POLL_MS[Math.min(Math.max(tries, 1), VERIFY_POLL_MS.length - 1)];
}

/** 未到终态时的进度文案；`pending` 可取消、`running` 不可（与后端只允许取消 pending 一致）。 */
export function progressText(status: string): { text: string; cancellable: boolean } {
  if (status === "running") return { text: "正在在线校验账号信息…", cancellable: false };
  return { text: "已排上在线校验，等待执行…", cancellable: status === "pending" };
}

/** 终态文案。rejected 的 error 为空时**不编造原因**。 */
export function finishText(
  status: string,
  error?: string | null,
  phone?: string | null,
): { text: string; bad: boolean } {
  if (status === "done") {
    return {
      text: `在线校验通过${phone ? `（${phone}）` : ""}。等待管理员审核后即可自动签到。`,
      bad: false,
    };
  }
  if (status === "cancelled") {
    return { text: "已取消本次在线校验。账号仍已提交，管理员审核时可手动核对。", bad: false };
  }
  return { text: `在线校验未通过${error ? `：${error}` : ""}。可修正账号信息后重新提交。`, bad: true };
}

/** 轮询到上界（仍未终态）时的收尾文案。 */
export function exhaustedText(): string {
  return "在线校验仍在进行，可稍后刷新本页查看结果。";
}

export function queryingText(): string {
  return "正在查询校验状态…";
}

/** 查询失败文案：按状态码区分，其余归为通用失败（都要停止轮询）。 */
export function queryErrorText(status?: number): string {
  if (status === 404) return "校验任务已不存在或已过期";
  if (status === 403) return "无权查看该校验任务";
  return "校验状态查询失败，请稍后刷新查看";
}

/** 取消失败文案；409 由调用方改走"重查一次真实状态"（任务已被后台抢走开工）。 */
export function cancelErrorText(message?: string | null): string {
  return `取消失败：${message || "请稍后重试"}`;
}

export const VERIFY_CANCEL_CONFLICT = 409;
