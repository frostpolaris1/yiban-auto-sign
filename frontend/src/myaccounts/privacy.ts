/**
 * 二段资料与暴露开关的前端口径（纯函数，Vitest 单测）。
 *
 * 契约见 `docs/dev/api-contract.md` 的 `PUT /api/my-accounts/<idx>/profile` 与
 * `PUT /api/accounts/<idx>/profile`（PR #201 后端）。
 *
 * 三条不可改写的语义：
 * 1. **全部默认不暴露**：没有下发 `profile` 时五个开关全关、五个输入全空。
 * 2. **视图只下发开着的字段**（我的视图与管理详情同口径；管理列表永不下发）。
 *    因此关闭字段的既有内容在页面上不可见——保存时**只提交用户改动过的键**，
 *    否则空串会被后端当成显式清空，把库里的隐藏值冲掉。
 * 3. **子集合并**：`profile` / `expose` 未给的键不动；`phone` 是防错位对齐令牌
 *    （管理面 fail-closed，本人面携带时同口径校验）。
 *
 * 暴露文案如实描述 #201 的实际出口：开＝本人视图＋管理详情可见，管理列表永不出现；
 * 关＝任何列表或详情都不显示（管理端维护写口仍可改，但不构成读取面）。
 */

import { MISSING_DETAIL } from "./accountform";

export type ProfileField = "real_name" | "student_id" | "college" | "class_name" | "email";

export type PrivacyVariant = "user" | "admin";

export interface PrivacyFieldMeta {
  key: ProfileField;
  label: string;
  maxlength: number;
  placeholder: string;
}

/** 词表与顺序对齐后端 `accounts.PROFILE_FIELDS`（顺序即页面渲染顺序）。 */
export const PRIVACY_FIELDS: readonly PrivacyFieldMeta[] = [
  { key: "real_name", label: "姓名", maxlength: 50, placeholder: "如：张三" },
  { key: "student_id", label: "学号", maxlength: 50, placeholder: "如：20250001" },
  { key: "college", label: "学院", maxlength: 50, placeholder: "如：计算机学院" },
  { key: "class_name", label: "班级", maxlength: 50, placeholder: "如：软件2401" },
  { key: "email", label: "邮箱", maxlength: 254, placeholder: "如：name@example.com" },
];

export interface PrivacyState {
  profile: Record<ProfileField, string>;
  expose: Record<ProfileField, boolean>;
}

export function emptyPrivacyState(): PrivacyState {
  const profile = {} as Record<ProfileField, string>;
  const expose = {} as Record<ProfileField, boolean>;
  for (const f of PRIVACY_FIELDS) {
    profile[f.key] = "";
    expose[f.key] = false;
  }
  return { profile, expose };
}

/**
 * 从服务端下发的 `profile`（只含已暴露字段）还原表单态。
 * 键存在＝开关打开；值为空串也仍算打开（后端 `exposed_profile` 对开着的空字段照样带键）。
 */
export function privacyFromView(profile?: Record<string, string> | null): PrivacyState {
  const state = emptyPrivacyState();
  if (!profile || typeof profile !== "object") return state;
  for (const f of PRIVACY_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(profile, f.key)) continue;
    const raw = profile[f.key];
    state.profile[f.key] = typeof raw === "string" ? raw : String(raw ?? "");
    state.expose[f.key] = true;
  }
  return state;
}

export interface PrivacyDiff {
  profile: Partial<Record<ProfileField, string>>;
  expose: Partial<Record<ProfileField, boolean>>;
  hasChanges: boolean;
  /** 本次由关变开的字段（按词表顺序），确认文案只列这些。 */
  newlyExposed: ProfileField[];
}

function trimmed(value: string | undefined): string {
  return String(value ?? "").trim();
}

/** 与基线比较，只产出改动过的键（未改动的键绝不出现，隐藏值因此不会被空串覆盖）。 */
export function privacyDiff(state: PrivacyState, initial: PrivacyState): PrivacyDiff {
  const profile: Partial<Record<ProfileField, string>> = {};
  const expose: Partial<Record<ProfileField, boolean>> = {};
  const newlyExposed: ProfileField[] = [];
  for (const f of PRIVACY_FIELDS) {
    const next = trimmed(state.profile[f.key]);
    if (next !== trimmed(initial.profile[f.key])) profile[f.key] = next;
    const on = !!state.expose[f.key];
    if (on !== !!initial.expose[f.key]) expose[f.key] = on;
    if (on && !initial.expose[f.key]) newlyExposed.push(f.key);
  }
  return {
    profile,
    expose,
    hasChanges: Object.keys(profile).length > 0 || Object.keys(expose).length > 0,
    newlyExposed,
  };
}

/** 长度上限与后端同口径（strip 后按码点计；email 254，其余 50）。 */
export function validatePrivacy(state: PrivacyState): string | null {
  for (const f of PRIVACY_FIELDS) {
    const len = [...trimmed(state.profile[f.key])].length;
    if (len > f.maxlength) return `${f.label}过长（最多 ${f.maxlength} 字）`;
  }
  return null;
}

export type PrivacyPayload = {
  profile?: Partial<Record<ProfileField, string>>;
  expose?: Partial<Record<ProfileField, boolean>>;
  phone?: string;
};

/**
 * 构造维护端点请求体。无改动返回 `null`（调用方跳过请求，避免空体 400）。
 * `requirePhone` 为真（管理面 fail-closed）时缺完整号拒提交。
 */
export function buildPrivacyPayload(
  state: PrivacyState,
  initial: PrivacyState,
  phone: string,
  opts: { requirePhone?: boolean } = {},
): PrivacyPayload | { error: string } | null {
  const invalid = validatePrivacy(state);
  if (invalid) return { error: invalid };
  const diff = privacyDiff(state, initial);
  if (!diff.hasChanges) return null;
  const token = String(phone || "").trim();
  if (opts.requirePhone && !token) return { error: MISSING_DETAIL };
  const payload: PrivacyPayload = {};
  if (Object.keys(diff.profile).length) payload.profile = diff.profile;
  if (Object.keys(diff.expose).length) payload.expose = diff.expose;
  if (token) payload.phone = token;
  return payload;
}

export function privacyEndpoint(kind: "my" | "admin", idx: number): string {
  return kind === "my" ? `/api/my-accounts/${idx}/profile` : `/api/accounts/${idx}/profile`;
}

export function privacyTitle(): string {
  return "二段：隐私资料与暴露";
}

export function exposureHint(on: boolean, variant: PrivacyVariant): string {
  if (on) {
    return variant === "admin"
      ? "开启：显示在账号详情；管理列表始终不显示。"
      : "开启：显示在你的账号页与管理详情；管理列表始终不显示。";
  }
  return "关闭：不显示在任何列表或详情；内容仍保存，打开开关后可见。";
}

export function privacyConfirmBody(diff: PrivacyDiff): string {
  if (diff.newlyExposed.length) {
    const labels = PRIVACY_FIELDS.filter((f) => diff.newlyExposed.includes(f.key)).map((f) => f.label);
    return `即将暴露：${labels.join("、")}。\n暴露后出现在你的账号视图与管理详情；管理列表始终不显示。确认保存？`;
  }
  return "将更新隐私资料与暴露开关。\n关闭的字段不在任何列表或详情显示。确认保存？";
}

/** 我的视图/管理详情里的只读展示行：按词表顺序拼有内容的已暴露字段。 */
export function profileLine(profile?: Record<string, string> | null): string {
  if (!profile || typeof profile !== "object") return "";
  const parts: string[] = [];
  for (const f of PRIVACY_FIELDS) {
    const value = trimmed(profile[f.key]);
    if (value) parts.push(`${f.label}：${value}`);
  }
  return parts.join(" · ");
}
