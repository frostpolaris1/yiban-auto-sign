/**
 * 外壳层桥接（core.js 的类型化入口）。
 *
 * **不实现任何逻辑，只做类型收窄**——这些能力的唯一实现是 `web/static/js/core.js`
 * （Vue 页与 legacy 页跑在同一套外壳里，core.js 必然已加载）。
 *
 * 为什么坚持复用而不是在 TS 里重写：
 *   · 口令策略：`tests/test_rekey_key_source.py` 是**跨层对拍元测试**——它要求
 *     `const PW_CLASS_PATTERNS = [...]` 在全站聚合前端源码里**恰好出现一处**、且与后端
 *     `_PASSWORD_CLASS_PATTERNS` 同序同串（注释原话："既不得退回逐处内联，也不得删掉"）。
 *     在 TS 里再声明一份就是制造第二个源，且可能让该断言失败。
 *   · 确认弹窗 / toast / 受门禁提交：与 shell 的模态栈、焦点管理、门禁档位（只存在于后端）
 *     耦合，重写必然漂移。
 *
 * 降级策略：外壳缺失（纯组件单测、或将来独立运行）时给出**安全默认**——确认框一律
 * 返回 false（宁可什么都不做，也不误触删除/注销），其余为空操作。
 */

export interface Destroyable {
  (): void;
}

export interface ShellToast {
  (message: string, isError?: boolean): void;
  success(message: string): void;
  error(message: string): void;
  warning(message: string): void;
  info(message: string): void;
}

export interface ConfirmOptions {
  title?: string;
  body?: string;
  confirmText?: string;
  danger?: boolean;
}

export interface DangerousSubmitOptions {
  method: string;
  path: string;
  body?: unknown;
  desc?: string;
}

export interface PasswordModalOptions {
  builtinAdmin?: boolean;
}

export interface Shell {
  confirmDialog(options: ConfirmOptions): Promise<boolean>;
  /** 受门禁提交：先不带凭据发，后端回 reason 才补口令；取消时 reject 的 error 带 `canceled`。 */
  dangerousSubmit(options: DangerousSubmitOptions): Promise<unknown>;
  toast: ShellToast;
  openConfirmPasswordModal(desc: string, onSubmit: (password: string) => void, onCancel?: () => void): void;
  changePassword: { open(options: PasswordModalOptions): unknown };
  passwordPolicyOk(password: string): boolean;
  passwordPolicyOkAdmin(password: string): boolean;
  PW_POLICY_HINT: string;
  PW_ADMIN_HINT: string;
  prefs: { ownerEmailVisible(): boolean; setOwnerEmailVisible(v: boolean): void };
  BASE?: string;
}

interface RawShell {
  confirmDialog?: (o: ConfirmOptions) => Promise<boolean>;
  dangerousSubmit?: (o: DangerousSubmitOptions) => Promise<unknown>;
  toast?: ShellToast;
  openConfirmPasswordModal?: Shell["openConfirmPasswordModal"];
  changePassword?: Shell["changePassword"];
  passwordPolicyOk?: Shell["passwordPolicyOk"];
  passwordPolicyOkAdmin?: Shell["passwordPolicyOkAdmin"];
  PW_POLICY_HINT?: string;
  PW_ADMIN_HINT?: string;
  prefs?: Shell["prefs"];
  BASE?: string;
}

function raw(): RawShell {
  return (typeof window !== "undefined" ? (window as { YB?: RawShell }).YB : undefined) ?? {};
}

/** 是否运行在完整外壳里（Vue 页在生产恒为 true；组件单测里为 false）。 */
export function hasShell(): boolean {
  return typeof raw().confirmDialog === "function";
}

export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  const fn = raw().confirmDialog;
  if (!fn) return Promise.resolve(false); // 安全默认：不确认就不动作
  return fn(options);
}

export function dangerousSubmit(options: DangerousSubmitOptions): Promise<unknown> {
  const fn = raw().dangerousSubmit;
  if (!fn) return Promise.reject(new Error("外壳未就绪，无法执行受门禁操作"));
  return fn(options);
}

export function toast(): ShellToast {
  const t = raw().toast;
  if (t) return t;
  const noop = (() => undefined) as unknown as ShellToast;
  return Object.assign(noop, {
    success: noop as unknown as (m: string) => void,
    error: noop as unknown as (m: string) => void,
    warning: noop as unknown as (m: string) => void,
    info: noop as unknown as (m: string) => void,
  });
}

export function openConfirmPasswordModal(
  desc: string,
  onSubmit: (password: string) => void,
  onCancel?: () => void,
): void {
  const fn = raw().openConfirmPasswordModal;
  if (fn) fn(desc, onSubmit, onCancel);
}

/** 修改密码弹窗（共享唯一实现；改密会轮换会话，成功后由该组件跳登录页）。 */
export function openChangePassword(options: PasswordModalOptions): void {
  const fn = raw().changePassword?.open;
  if (fn) fn(options);
}

export function passwordPolicyOk(password: string): boolean {
  const fn = raw().passwordPolicyOk;
  return fn ? fn(password) : false;
}

export function passwordPolicyOkAdmin(password: string): boolean {
  const fn = raw().passwordPolicyOkAdmin;
  return fn ? fn(password) : false;
}

export function passwordHint(admin: boolean): string {
  const r = raw();
  if (admin) return r.PW_ADMIN_HINT ?? "至少 12 位，且包含大写字母、小写字母、数字、符号中的至少三类";
  return r.PW_POLICY_HINT ?? "至少 10 位，且包含大小写字母、数字、符号中的至少两类";
}

export function ownerEmailVisible(): boolean {
  const fn = raw().prefs?.ownerEmailVisible;
  return fn ? fn() : false;
}

export function setOwnerEmailVisible(v: boolean): void {
  const fn = raw().prefs?.setOwnerEmailVisible;
  if (fn) fn(v);
}

/** 子路径部署前缀（theme_boot 写入）。 */
export function shellBase(): string {
  const b = raw().BASE;
  return typeof b === "string" ? b : "";
}
