/**
 * 账号新增/编辑弹窗的口径（纯函数，Vitest 单测）。
 *
 * **语义逐条对齐 legacy `web/static/js/components/account-form.js`**，其中三处是
 * 「看似可简化、实为故意设计」的规则，迁移时最容易被改坏，故在此显式化并测试：
 *
 * 1. **完整手机号只经内存暂存**：管理端编辑时列表下发的是**打码号**，输入框显示的也是
 *    打码号；完整号只在提交那一刻从列表项取用，绝不回显进 DOM/常驻文本（隐私口径）。
 *    ⇒ 打码号 + 无完整号可用 = 拒绝提交（"无法获取账号完整信息"），而不是让用户手填。
 * 2. **乐观锁快照（`_snapshot`）**：管理端编辑前先取一次 detail，把
 *    {name, phone, phone_model, status, deleted} 作为指纹随提交上传；后端发现与当前行不符
 *    会要求二次确认（副标题里对用户的承诺"若被他人改过会先提示你确认"）。
 *    ⇒ 取不到快照就不允许提交，避免"基于过期视图覆盖别人改动"。
 * 3. **凭据门禁判定（`credsWritten`）**：只有"写了新密码"或"改绑了手机号"才算改写他人
 *    易班凭据、才走受门禁提交；只改名称/设备型号不进门禁（与后端 `creds_written` 同口径）。
 *
 * 另有两条**已知但刻意不动**的地方（属契约，不是缺陷）：
 * · 手机号/识别码只做必填与长度限制，格式交后端判定（前端不做正则前置，避免与后端
 *   口径漂移后误拒合法值）；
 * · 绑定已注册用户（非手填）时仍会带一个空的 `initial_password` 键（后端忽略），
 *   改动这个键的存在性会变更提交契约，故保持原样。
 */

export type FormVariant = "user" | "admin";

/** 提交 `phone_code` 时表示"清除已配置识别码"的哨兵值（与后端约定）。 */
export const CODE_CLEAR = "__clear__";

/** 绑定用户下拉的固定条目与分组头（顺序即渲染顺序）。 */
export interface SelectItem {
  v?: string;
  t?: string;
  group?: string;
  empty?: string;
}

export function emailBaseItems(): SelectItem[] {
  return [
    { v: "", t: "不绑定（管理员自有账号）" },
    { v: "__manual__", t: "手填邮箱（未注册）" },
    { group: "已注册用户（无账号）" },
  ];
}

/**
 * 可绑定用户下拉项：只列"还没有账号"的用户。
 * 可见文本一律用服务端下发的**遮罩** `display`（`email.split("@")[0]` 是已被废弃的
 * 第二份归属展示规则，号形态时会把完整手机号外显）；完整邮箱只进 value、随请求体提交。
 */
export function availableUserItems(
  users: Array<{ email?: string; display?: string; account_count?: number }>,
): SelectItem[] {
  const items = emailBaseItems();
  const list = users.filter((u) => (u.account_count || 0) === 0);
  if (!list.length) {
    items.push({ empty: "（暂无）" });
    return items;
  }
  for (const u of list) items.push({ v: String(u.email || ""), t: String(u.display || "") });
  return items;
}

/** 字段文案（两套口径各自沿用既有措辞；改一处别忘了另一处）。 */
export const FORM_TEXTS = {
  user: {
    nameLabel: "名称 / 备注（可选）",
    namePlaceholder: "如：我的易班账号",
    nameHelp: "会显示给管理员，便于审核。",
    phoneLabel: "易班手机号",
    phonePlaceholder: "登录易班的手机号",
    passwordLabel: "易班密码",
    passwordNewPlaceholder: "用于自动登录签到",
    modelLabel: "设备型号（可选，不清楚就留空）",
    modelHelp: "仅在提示「请使用授权设备」时填写，不确定就留空。",
    codeLabel: "设备识别码（可选，不清楚就留空）",
    codeHelp: "仅在提示「请使用授权设备」时填写，不确定就留空。",
    phoneEditHelp: "",
  },
  admin: {
    nameLabel: "名称（选填）",
    namePlaceholder: "如：电力123示例站",
    nameHelp: "留空时在列表里按顺序显示为「账号1」「账号2」，便于区分。",
    phoneLabel: "手机号",
    phonePlaceholder: "易班登录手机号",
    passwordLabel: "密码",
    passwordNewPlaceholder: "易班登录密码",
    modelLabel: "设备型号（选填）",
    modelHelp: "学校开启设备绑定时建议填写；不确定就留空。",
    codeLabel: "设备识别码（选填）",
    codeHelp: "64 位十六进制串；学校开启设备绑定时建议填写。",
    phoneEditHelp:
      "为保护隐私，手机号已打码显示；如需修改请填写完整新号码。若这条账号在你编辑期间被别人改过，保存时会先提示你确认。",
  },
} as const;

export function formTexts(variant: FormVariant) {
  return FORM_TEXTS[variant];
}

/** 打码号特征（列表下发形态）。 */
export const MASK_MARK = "****";

export function isMaskedPhone(visible: string): boolean {
  return String(visible || "").indexOf(MASK_MARK) !== -1;
}

/**
 * 解析提交用手机号。返回 `{ value }` 或 `{ error }`：
 * · 打码号 + 内存里有完整号 → 用内存值（完整号不回显、不驻留）；
 * · 打码号 + 内存无值 → 拒提交（口径 1）；
 * · 未打码 → 用用户输入值。
 */
export function resolvePhone(visible: string, fullFromMemory?: string | null): { value: string } | { error: string } {
  const v = String(visible || "").trim();
  if (isMaskedPhone(v)) {
    if (!fullFromMemory) return { error: MISSING_DETAIL };
    return { value: fullFromMemory };
  }
  if (!v) return { error: "请填写易班手机号" };
  return { value: v };
}

export const MISSING_DETAIL = "无法获取账号完整信息，请刷新页面后重试";

export interface AccountFormInput {
  name: string;
  phoneVisible: string;
  /** 管理端编辑时来自列表项的完整号（仅内存，不回显） */
  phoneFull?: string | null;
  password: string;
  phoneModel: string;
  phoneCode: string;
  /** 是否处于"清除已配置识别码"态 */
  clearCode: boolean;
  /** 绑定用户下拉的当前值（仅 allowEmail 且非编辑时有意义） */
  email?: string;
  /** 手填邮箱（下拉选 __manual__ 时生效） */
  manualEmail?: string;
  initialPassword?: string;
}

export interface AccountPayload {
  name: string;
  phone: string;
  password: string;
  phone_model: string;
  phone_code: string;
  email?: string;
  initial_password?: string;
  _snapshot?: string;
}

export interface BuildResult {
  payload?: AccountPayload;
  /** 校验失败时的用户可见文案（同时指明应聚焦的字段，便于页面 setFocus） */
  error?: string;
  focus?: "phone" | "password" | "manualEmail" | "initialPassword";
}

/**
 * 构造提交体并做（按顺序的）本地校验。
 *
 * `allowEmail` 为真且非编辑时，还会校验"手填邮箱"分支：必须填邮箱，且初始密码须过
 * 口令策略（策略判定由调用方注入 `policyOk`，因为唯一实现住在 core.js）。
 */
export function buildAccountPayload(
  input: AccountFormInput,
  opts: {
    editing: boolean;
    allowEmail?: boolean;
    snapshot?: string | null;
    /** 是否要求先取到 detail 快照（管理端编辑 = true；用户端编辑自己的账号 = false，与 legacy 的 opts.detail 对应） */
    requiresSnapshot?: boolean;
    policyOk?: (pw: string) => boolean;
    policyHint?: string;
  },
): BuildResult {
  // 口径 2：要求快照却没拿到 → 不得提交（防基于过期视图覆盖别人的改动）
  if (opts.editing && opts.requiresSnapshot && !opts.snapshot) {
    return { error: MISSING_DETAIL };
  }

  const phone = resolvePhone(input.phoneVisible, input.phoneFull);
  if ("error" in phone) {
    return { error: phone.error, focus: phone.error === MISSING_DETAIL ? undefined : "phone" };
  }
  if (!opts.editing && !input.password) {
    return { error: "请填写易班密码", focus: "password" };
  }

  const payload: AccountPayload = {
    name: String(input.name || "").trim(),
    phone: phone.value,
    password: input.password || "",
    phone_model: String(input.phoneModel || "").trim(),
    phone_code: input.clearCode ? CODE_CLEAR : String(input.phoneCode || "").trim(),
  };

  if (opts.allowEmail && !opts.editing) {
    const manual = input.email === "__manual__";
    const email = manual ? String(input.manualEmail || "").trim() : String(input.email || "");
    if (manual) {
      if (!email) return { error: "请填写邮箱", focus: "manualEmail" };
      const pw = String(input.initialPassword || "");
      if (!(opts.policyOk ? opts.policyOk(pw) : false)) {
        return { error: `初始密码${opts.policyHint ?? ""}`, focus: "initialPassword" };
      }
    }
    if (email) payload.email = email;
    // 契约保留：非手填分支也带一个空 initial_password（后端忽略）；见模块头「刻意不动」
    payload.initial_password = String(input.initialPassword || "");
  }

  if (opts.editing && opts.snapshot) payload._snapshot = opts.snapshot;

  return { payload };
}

/** 乐观锁快照指纹的载荷形状（与后端比对口径一致）。 */
export function makeSnapshot(acc: {
  name?: string;
  phone?: string;
  phone_model?: string;
  status?: string;
  deleted?: boolean;
}): string {
  return JSON.stringify({
    name: acc.name,
    phone: acc.phone,
    phone_model: acc.phone_model,
    status: acc.status,
    deleted: acc.deleted,
  });
}

/**
 * 口径 3：本次提交是否算"改写他人易班凭据"（决定是否走受门禁提交）。
 * 依据后端 `creds_written`：写了非空新密码，或手机号与原号不同（改绑）。
 * 快照缺失时无法判"改绑"，退化为只看密码。
 */
export function credsWritten(
  payload: Pick<AccountPayload, "password" | "phone">,
  opts: { editing: boolean; snapshot?: string | null },
): boolean {
  if (!opts.editing) return false;
  const wrotePassword = String(payload.password || "").trim() !== "";
  let origPhone: string | null = null;
  if (opts.snapshot) {
    try {
      origPhone = JSON.parse(opts.snapshot).phone ?? null;
    } catch {
      origPhone = null;
    }
  }
  const rebound = origPhone != null && payload.phone !== origPhone;
  return wrotePassword || rebound;
}

export const CREDS_GATE_DESC =
  "本次修改会改写该账号的易班凭据（换了密码或改绑手机号），请输入当前管理员密码确认。";

export function submitLabel(opts: { editing: boolean; variant: FormVariant }): string {
  if (opts.editing) return "保存修改";
  return opts.variant === "user" ? "提交账号" : "添加账号";
}

export function formTitle(opts: { editing: boolean; variant: FormVariant; index: number | null }): string {
  if (opts.editing) {
    return opts.variant === "user" ? "编辑我的易班账号" : `编辑账号 #${(opts.index ?? 0) + 1}`;
  }
  return opts.variant === "user" ? "提交我的易班账号" : "添加账号";
}

export function formSubtitle(opts: { editing: boolean; variant: FormVariant }): string {
  if (opts.editing) {
    return opts.variant === "user" ? "修改后需重新提交审核。" : "手机号已打码显示；不改动则按原号提交。";
  }
  return opts.variant === "user"
    ? "提交后等待管理员审核，通过即自动签到。"
    : "不绑定用户＝管理员自有账号，保存后立即生效；绑定用户＝进入待审核队列。";
}

export const PASSWORD_UNCHANGED_PLACEHOLDER = "留空表示不修改密码";
export const CODE_UNCHANGED_PLACEHOLDER = "留空表示不修改（已配置）";
export const CODE_PLACEHOLDER = "64 位十六进制识别码";
export const CODE_CLEARED_PLACEHOLDER = "提交后将清除已配置识别码";
export const CODE_CLEAR_BTN = "清除已配置识别码";
export const CODE_CLEAR_CANCEL_BTN = "取消清除";
