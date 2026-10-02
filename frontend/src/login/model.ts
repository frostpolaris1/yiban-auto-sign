/**
 * 登录 / 注册页口径（纯函数，无 DOM、无网络、无全局）。
 *
 * 为什么把口令策略**注入**而不是在这里判：策略的唯一实现是 `core.js` 的
 * `PW_CLASS_PATTERNS / PW_MIN_LEN / PW_MIN_CLASSES`（由 `tests/test_rekey_key_source.py`
 * 跨层对拍到后端 `_PASSWORD_CLASS_PATTERNS / PASSWORD_MIN_LEN / _PASSWORD_MIN_CLASSES`）。
 * 这里只接受一个 `PasswordPolicy` 接口（`frontend/src/login/main.ts` 从外壳桥接进来），
 * 于是口径可单测、而策略仍只有一处定义。
 */

/** 口令策略接口（实现在 core.js，经 lib/shell.ts 桥接）。 */
export interface PasswordPolicy {
  minLen: number;
  minClasses: number;
  /** 满足了几类字符 */
  classes(v: string): number;
  /** 策略整句提示（如「至少 10 位，且包含…中的至少两类」） */
  hint: string;
}

/* ---------------- 邮箱 ---------------- */

/** `@` 前最多 32 字符（后端同口径），整体最多 64。 */
export const EMAIL_MAX = 64;
export const EMAIL_LOCAL_MAX = 32;
export const EMAIL_RE = /^[\w.+-]{1,32}@[\w-]+(\.[\w-]+)+$/;

export interface FieldCheck {
  invalid: boolean;
  /** invalid 时展示在字段下方的文案（字段级校验优先于顶部错误框） */
  message: string;
}

export const EMAIL_FORMAT_MSG = "邮箱格式不正确（示例：name@example.com）";
export const EMAIL_LOCAL_TOO_LONG_MSG = "邮箱用户名部分过长（最多 32 字符）";
export const PW_MISMATCH_MSG = "两次输入的密码不一致";
export const AGREE_REQUIRED_MSG = "请先阅读并同意《用户协议》和《隐私政策》";

/**
 * 邮箱校验：空值不算错（提交由 required 兜底），非空才判格式与长度。
 *
 * 两种失败文案分开：本地部超长是**独立**于格式的原因（用户把邮箱前缀写太长时，
 * 只说"格式不正确"会让人反复检查域名）。判据与后端 `_EMAIL_RE` 同形。
 */
export function checkEmail(raw: string): FieldCheck {
  const v = String(raw ?? "").trim();
  if (v === "") return { invalid: false, message: EMAIL_FORMAT_MSG };
  const local = v.includes("@") ? v.split("@")[0] : "";
  if (local.length > EMAIL_LOCAL_MAX) {
    return { invalid: true, message: EMAIL_LOCAL_TOO_LONG_MSG };
  }
  return {
    invalid: !(EMAIL_RE.test(v) && v.length <= EMAIL_MAX),
    message: EMAIL_FORMAT_MSG,
  };
}

/* ---------------- 口令 ---------------- */

/**
 * 口令校验 + 即时提示文案（`当前 N 位、M 类`）。
 *
 * 空值不算错（同邮箱：提交由 required 兜底），非空才判下限——否则用户刚聚焦、
 * 一个字都没输就先被标红。
 */
export function checkPassword(raw: string, policy: PasswordPolicy): FieldCheck {
  const v = String(raw ?? "");
  const classes = policy.classes(v);
  const message = `密码${policy.hint}（当前 ${v.length} 位，${classes} 类）`;
  if (v === "") return { invalid: false, message };
  return { invalid: v.length < policy.minLen || classes < policy.minClasses, message };
}

/** 两次一致：确认框为空时不提示（提交由 required 兜底），非空即比对。 */
export function checkConfirm(password: string, confirm: string): FieldCheck {
  const v = String(confirm ?? "");
  return { invalid: v !== "" && v !== password, message: PW_MISMATCH_MSG };
}

/* ---------------- 提交闸门 ---------------- */

export interface RegisterInput {
  email: string;
  password: string;
  confirm: string;
  agree: boolean;
  policy: PasswordPolicy;
}

export interface RegisterGate {
  /** 是否被挡下（被挡时字段级错误已就位，页面只需保持不提交） */
  blocked: boolean;
  /** 顶部错误框文案；空串表示"错误已在字段下方显示，不弹顶部框" */
  topMessage: string;
  email: FieldCheck;
  password: FieldCheck;
  confirm: FieldCheck;
}

/**
 * 注册提交闸门：先查协议勾选（唯一的"非字段级"前置），再逐字段校验。
 *
 * 勾选失败**必须弹顶部错误框**：它不对应任何单个字段，只在字段下方提示用户会找不到原因。
 * 字段校验失败则**只标字段**（legacy 同此：字段级优先，不重复弹顶部框）。
 */
export function registerGate(input: RegisterInput): RegisterGate {
  const email = checkEmail(input.email);
  const password = checkPassword(input.password, input.policy);
  const confirm = checkConfirm(input.password, input.confirm);
  if (!input.agree) {
    return { blocked: true, topMessage: AGREE_REQUIRED_MSG, email, password, confirm };
  }
  return {
    blocked: email.invalid || password.invalid || confirm.invalid,
    topMessage: "",
    email,
    password,
    confirm,
  };
}

/* ---------------- 响应分支 ---------------- */

export interface LoginResponse {
  ok?: boolean;
  role?: string;
  recoverable?: boolean;
  msg?: string;
  error?: string;
}

export type LoginOutcome =
  | { kind: "recoverable"; message: string }
  | { kind: "ok"; path: string }
  | { kind: "error"; message: string };

/**
 * 登录响应 → 页面动作。**`recoverable` 必须先于 `ok` 判断**：注销冷静期的响应同时带
 * `ok:true`，先查 ok 就会直接跳转，恢复入口成死代码（legacy 注释里记过这个坑）。
 */
export function loginOutcome(data: LoginResponse | null | undefined): LoginOutcome {
  if (data && data.recoverable) {
    return { kind: "recoverable", message: data.msg || "账号已注销，7 天内可恢复" };
  }
  if (data && data.ok) return { kind: "ok", path: landingPath(data.role) };
  return { kind: "error", message: (data && data.error) || "登录失败，请重试" };
}

/** 恢复（注销冷静期）响应 → 页面动作；失败时页面还要把恢复按钮收起来。 */
export function restoreOutcome(data: LoginResponse | null | undefined): LoginOutcome {
  if (data && data.ok) return { kind: "ok", path: landingPath(data.role) };
  return { kind: "error", message: (data && data.error) || "恢复失败，请稍后再试" };
}

/**
 * 登录成功后的落点：管理员回后台总览，其余（含注册即登录）回签到日历。
 *
 * 用户端首页是日历而非账号页——进站第一眼要看今天的签到结果，账号是低频维护对象。
 */
export function landingPath(role: string | undefined): string {
  return role === "admin" ? "/data/dashboard" : "/user/calendar";
}

/** 网络/外壳异常的统一文案（与 legacy 逐字一致）。 */
export function networkErrorText(e: unknown): string {
  const msg = (e as { message?: string } | null)?.message;
  return msg || "网络异常，请检查网络后重试";
}

/* ---------------- 页签 ---------------- */

export type AuthMode = "login" | "register";

export const AUTH_TITLES: Record<AuthMode, [string, string]> = {
  login: ["登录", "使用注册邮箱进入签到管理面板。"],
  register: ["注册", "创建账号后即可添加并管理易班签到。"],
};

/**
 * 切页签是否允许：注册暂停时不得切入注册（页签已禁用，这里是键盘/脚本路径的兜底）。
 */
export function canSwitchTo(mode: AuthMode, registrationPaused: boolean): boolean {
  return !(mode === "register" && registrationPaused);
}
