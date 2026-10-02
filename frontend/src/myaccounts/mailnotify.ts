/**
 * 邮件提醒开关的口径（纯函数，Vitest 单测）。
 *
 * **语义逐条对齐 legacy `web/static/js/components/my-mail-notify.js`**。
 * 两条写入路径不同、且**主管理员关闭方向受门禁**——这是最容易被"顺手统一"改坏的地方：
 *   · user          → PUT /api/my-mail-notify {enabled}（本人开关，不进门禁，失败回滚开关）
 *   · builtin-admin → GET/PUT /api/mail-config {admin_notify}；**只有关闭**走门禁
 *                     （先不带凭据发，后端回 reason 才补口令；取消弹窗不算失败）
 * 提示文案一律不含手机号/邮箱等 PII（由单测钉住）。
 */

export type MailVariant = "user" | "builtin-admin";

export function isBuiltinAdminVariant(variant: MailVariant): boolean {
  return variant === "builtin-admin";
}

/** 普通用户保存成功后的提示（TTL 3s，由页面负责计时）。 */
export function userTip(on: boolean): string {
  return on ? "已开启：签到失败时将邮件提醒你" : "已关闭：不再发送签到失败邮件";
}

/** 主管理员保存成功后的提示。 */
export function adminTip(on: boolean): string {
  return on ? "已开启接收邮件提醒" : "已关闭接收邮件提醒";
}

export function userSaveFailedTip(message?: string | null): string {
  return `保存失败：${message || "请稍后重试"}`;
}

export function adminSaveFailedTip(): string {
  return "保存失败，请稍后重试";
}

/** 关闭通道的门禁说明（口令提示由后端 reason 驱动，此处只是文案）。 */
export function adminGateDesc(): string {
  return "关闭主管理员告警邮件接收？关闭后你不再收到任何告警邮件。请输入当前管理员密码确认。";
}

/** 只有"关闭"需要门禁；开启是纯开启、直发。 */
export function needsGate(next: boolean): boolean {
  return next === false;
}

export const TIP_TTL_MS = 3000;
