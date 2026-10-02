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
  passwordClasses?: (v: string) => number;
  PW_MIN_LEN?: number;
  PW_MIN_CLASSES?: number;
  PW_POLICY_HINT: string;
  PW_ADMIN_HINT: string;
  openModal?: (o: ModalOptions) => unknown;
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
  passwordClasses?: (v: string) => number;
  PW_MIN_LEN?: number;
  PW_MIN_CLASSES?: number;
  PW_POLICY_HINT?: string;
  PW_ADMIN_HINT?: string;
  openModal?: (o: ModalOptions) => unknown;
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

/**
 * 口令「满足了几类字符」（core.js 唯一实现，判据 `PW_CLASS_PATTERNS`）。
 *
 * 为什么必须借外壳而不是在 TS 里数一遍：`tests/test_rekey_key_source.py` 是跨层对拍元测试，
 * 它要求 `const PW_CLASS_PATTERNS = [...]` 在**全站聚合前端源码里恰好出现一处**、且与后端
 * `_PASSWORD_CLASS_PATTERNS` 同序同串。在 TS 里再写一份正则数组就是制造第二个源。
 * 注册页的即时校验要显示"当前 N 位、M 类"，所以需要**类数**本身（不只布尔判定）。
 */
export function passwordClasses(password: string): number {
  const fn = (raw() as { passwordClasses?: (v: string) => number }).passwordClasses;
  return fn ? fn(password) : 0;
}

/** 口令长度下限（core.js 的 `PW_MIN_LEN`，与后端 `PASSWORD_MIN_LEN` 对拍）。 */
export function pwMinLen(): number {
  const v = (raw() as { PW_MIN_LEN?: number }).PW_MIN_LEN;
  return typeof v === "number" ? v : 10;
}

/** 口令类别下限（core.js 的 `PW_MIN_CLASSES`，与后端 `_PASSWORD_MIN_CLASSES` 对拍）。 */
export function pwMinClasses(): number {
  const v = (raw() as { PW_MIN_CLASSES?: number }).PW_MIN_CLASSES;
  return typeof v === "number" ? v : 2;
}

export interface ModalAction {
  label: string;
  variant?: string;
}

export interface ModalOptions {
  title?: string;
  size?: string;
  body?: Node;
  actions?: ModalAction[];
}

/**
 * 通用模态（**委托 `YB.openModal`**，core.js 的唯一实现）。
 *
 * 为什么不在 Vue 里另起一个模态：Esc 关闭、Tab 焦点圈定、滚动锁、关闭后焦点归还、
 * 与 toast 的层级关系——这些都在 core.js 的模态栈里，重写必然漂移；而登录页要弹的
 * 是**服务端渲染好的**协议正文（`<template id="doc-*">` 的惰性内容），它只能以 DOM
 * 节点形式交给模态（Vue 侧零 `v-html`，见该页组件说明）。
 */
export function openModal(options: ModalOptions): void {
  const fn = (raw() as { openModal?: (o: ModalOptions) => unknown }).openModal;
  if (fn) fn(options);
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

/**
 * 手机号展示脱敏（core.js 唯一实现，幂等）。
 * 服务端已是脱敏出口；页面对已脱敏值再走一遍是**防御性**的（与 legacy 事件行同做法），
 * 万一日后某条数据路径漏了服务端脱敏，这里仍不会把完整号渲染出去。
 */
export function maskPhone(phone: string): string {
  const fn = (raw() as { maskPhone?: (p: string) => string }).maskPhone;
  if (fn) return fn(phone);
  // 无外壳时的保守兜底：长度够就按"前 3 后 4"遮罩（与后端口径同形）
  const p = String(phone ?? "");
  if (p.indexOf("*") !== -1) return p;
  return p.length >= 7 ? `${p.slice(0, 3)}****${p.slice(-4)}` : p;
}

export function setOwnerEmailVisible(v: boolean): void {
  const fn = raw().prefs?.setOwnerEmailVisible;
  if (fn) fn(v);
}

/**
 * 慢请求的内容替换：加 `is-swapping` → 等退出过渡跑完（主元素 transitionend 或 SWAP_MS
 * 兜底，先到者）→ 回调换内容 → 摘类走进入过渡。
 *
 * **委托 `YB.swapOut`**（core.js 的唯一实现）："慢请求才淡出再换"这一口径在日历页与
 * legacy 各处共用，各写一份必然在时长与兜底上分叉。外壳缺失时直接执行回调（不做动画），
 * 保证组件单测与降级路径都能拿到内容。
 */
export function swapOut(nodes: Element | Element[] | null, cb: () => void): void {
  const fn = (raw() as { swapOut?: (n: unknown, cb: () => void) => void }).swapOut;
  if (fn) {
    fn(nodes, cb);
    return;
  }
  cb();
}

/** 交换窗口时长（毫秒），与 core.js 的 `SWAP_MS` 同口径；无外壳时取同一默认值。 */
export function swapMs(): number {
  const v = (raw() as { SWAP_MS?: number }).SWAP_MS;
  return typeof v === "number" ? v : 160;
}

export interface ShellApiError extends Error {
  status?: number;
  data?: unknown;
  isHttp?: boolean;
  isNetwork?: boolean;
}

/** 子路径部署前缀（theme_boot 写入）。 */
export function shellBase(): string {
  const b = raw().BASE;
  return typeof b === "string" ? b : "";
}

/**
 * 统一请求层：**委托 `YB.api`**（core.js 的唯一实现）——它承担 CSRF 头、401 清 token 重读
 * `/api/me` 再重试一次、并发 GET 去重、写请求的行分隔符前置拦截、以及错误归一化
 * （`Error` 携带 `.status` / `.data` / `.isHttp` / `.isNetwork`）。
 *
 * 为什么不在 TS 里另写一套：这些语义各自都有对应的后端契约与测试（例如行分隔符拦截与
 * 后端 `env_io.ENV_LINE_BREAK_CHARS` 同源），复制必然漂移。
 *
 * 外壳缺失（组件单测 / 将来独立运行）时退回 `fetch`：同源凭据 + JSON 解析 + 同样的错误
 * 形状。**不含 CSRF**，故仅适用于只读请求——写请求在外壳缺失时直接拒绝，避免"看起来成功
 * 实际上被后端 CSRF 拒绝"。
 */
export async function api<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  const shellApi = (raw() as { api?: (m: string, p: string, b?: unknown) => Promise<unknown> }).api;
  if (shellApi) return (await shellApi(method, path, body)) as T;

  if (method !== "GET") {
    throw new Error("外壳未就绪：写请求缺少 CSRF 保护，已拒绝执行");
  }
  const resp = await fetch(shellBase() + path, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  const text = await resp.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!resp.ok) {
    const message =
      data !== null && typeof data === "object" && "error" in (data as Record<string, unknown>)
        ? String((data as { error: unknown }).error)
        : `请求失败（${resp.status}）`;
    const err = new Error(message) as ShellApiError;
    err.status = resp.status;
    err.data = data;
    err.isHttp = true;
    throw err;
  }
  return data as T;
}

/** 判断错误是否为"用户取消了受门禁弹窗"（不是失败，调用方不应提示错误）。 */
export function isCanceled(e: unknown): boolean {
  return !!e && typeof e === "object" && (e as { canceled?: boolean }).canceled === true;
}

/** 取错误的状态码（core.js 归一化后的 `.status`）。 */
export function errorStatus(e: unknown): number | undefined {
  const s = (e as { status?: unknown })?.status;
  return typeof s === "number" ? s : undefined;
}

/** 取用户可见错误文案（core.js 已归一化 `.message`）。 */
export function errorMessage(e: unknown, fallback: string): string {
  const m = (e as { message?: unknown })?.message;
  return typeof m === "string" && m ? m : fallback;
}
