/**
 * **功能**
 * 注册表单的纯逻辑：就地校验、请求体与错误落点。
 *
 * **归属**
 * `features/auth` 域逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 请求体只含契约 `RegisterRequest` 的字段；确认口令只在前端比对。
 * 人机验证令牌只在拿到时附带，未配置验证时不出现该键。
 * 契约要求两个协议布尔都为 true、版本号等于当前 `RegistrationStatus`。
 * 开关关闭、未勾协议、字段非法时**零请求**。
 * 口令策略以服务端 `GET /api/auth/password-policy` 为权威；本地常量只是
 * 策略读不到时的兜底，两者都汇成 `EffectivePolicy` 单点使用。
 * 409 有两种来源：邮箱重复、协议版本过期。版本是否过期由调用方
 * 重新拉一次 `RegistrationStatus` 后与本函数比对，不靠读英文错误串。
 * 429 的秒数来自 `ApiError.retryAfter`，缺失时不给具体数字。
 */

import type { PasswordPolicy, RegisterRequest, RegistrationStatus } from '../../lib/api/auth'
import { ApiError, describeApiError } from '../../lib/api/client'

/** 本地兜底口径：与服务端默认策略一致，仅当策略接口读不到时使用。 */
export const PASSWORD_MIN_LENGTH = 10
export const PASSWORD_MIN_CLASSES = 2
export const PASSWORD_MAX_BYTES = 1024

export const PASSWORD_RULE = `至少 ${PASSWORD_MIN_LENGTH} 位，且包含大写、小写、数字、符号中的两类`

/** 生效策略：服务端值优先，缺省落本地常量。 */
export type EffectivePolicy = { minChars: number; minClasses: number; maxBytes: number }

export function resolvePolicy(policy: PasswordPolicy | null | undefined): EffectivePolicy {
  if (!policy) {
    return {
      minChars: PASSWORD_MIN_LENGTH,
      minClasses: PASSWORD_MIN_CLASSES,
      maxBytes: PASSWORD_MAX_BYTES,
    }
  }
  return {
    minChars: policy.min_chars,
    minClasses: policy.min_classes,
    maxBytes: policy.max_bytes ?? PASSWORD_MAX_BYTES,
  }
}

export function passwordRuleText(p: EffectivePolicy): string {
  return `至少 ${p.minChars} 位，且包含大写、小写、数字、符号中的 ${p.minClasses} 类`
}

/** UTF-8 字节数：中文口令按字节计，与服务端 max_bytes 同口径。 */
export function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).length
}

export type PasswordClasses = {
  upper: boolean
  lower: boolean
  digit: boolean
  symbol: boolean
}

/** 四类字符各命中与否；符号指非字母、非数字、非空白。 */
export function passwordClassList(value: string): PasswordClasses {
  return {
    upper: /[A-Z]/.test(value),
    lower: /[a-z]/.test(value),
    digit: /[0-9]/.test(value),
    symbol: /[^A-Za-z0-9\s]/.test(value),
  }
}

export function passwordClassCount(value: string): number {
  const classes = passwordClassList(value)
  return Number(classes.upper) + Number(classes.lower) + Number(classes.digit) + Number(classes.symbol)
}

/** 口令强度：分段条点亮的段数（0–4）与是否已满足当前策略。 */
export type PasswordStrength = { score: number; valid: boolean }

/**
 * 强度只按策略与字符构成推导，不猜熵：
 * 0 空；1 弱（长度与类别都没达标）；2 中（达标其一，尚未可用）；
 * 3 强（长度＋类别＋字节都满足，即策略可用）；4 很强（可用且四类齐全或更长）。
 * 颜色与文案由呈现层按 score 映射，这里只产出可单测的数值。
 */
export function passwordStrength(value: string, policy?: EffectivePolicy): PasswordStrength {
  const p = policy ?? resolvePolicy(null)
  if (value.length === 0) return { score: 0, valid: false }
  const lengthOk = value.length >= p.minChars
  const classes = passwordClassCount(value)
  const classesOk = classes >= p.minClasses
  const valid = lengthOk && classesOk && utf8Bytes(value) <= p.maxBytes
  if (!valid) {
    const coreMet = Number(lengthOk) + Number(classesOk)
    return { score: coreMet >= 1 ? 2 : 1, valid: false }
  }
  const diverse = classes >= 4 || value.length >= p.minChars + 6
  return { score: diverse ? 4 : 3, valid: true }
}

/** 强度档位文案；空串返回空，呈现层据此隐藏标签。 */
export function passwordStrengthLabel(score: number): string {
  return ['', '弱', '中', '强', '很强'][score] ?? ''
}

/** 返回第一条不满足的口令规则；空串通过。策略缺省落本地兜底值。 */
export function passwordIssue(value: string, policy?: EffectivePolicy): string | null {
  const p = policy ?? resolvePolicy(null)
  if (value.length === 0) return '请输入密码'
  if (utf8Bytes(value) > p.maxBytes) return `密码太长，最多 ${p.maxBytes} 字节`
  if (value.length < p.minChars) return `密码至少 ${p.minChars} 位`
  if (passwordClassCount(value) < p.minClasses) {
    return `密码需包含大写、小写、数字、符号中的 ${p.minClasses} 类`
  }
  return null
}

export type RegisterDraft = {
  email: string
  password: string
  confirm: string
  agree: boolean
}

export type RegisterField = 'email' | 'password' | 'confirm' | 'agree'

/** 字段名到就地错误文案。缺失即该字段通过。 */
export type RegisterErrors = Partial<Record<RegisterField, string>>

/** 只做形状判断，不比服务端更严：合法邮箱由服务端定论。 */
function emailShapeOk(value: string): boolean {
  return /^\S+@\S+$/.test(value)
}

/**
 * 就地校验。返回空对象即可以提交。
 * 顺序固定为邮箱、口令、确认、协议：提交失败时焦点取第一个出错字段。
 */
export function validateRegister(
  draft: RegisterDraft,
  status: RegistrationStatus | null | undefined,
  policy?: EffectivePolicy,
): RegisterErrors {
  const errors: RegisterErrors = {}
  const email = draft.email.trim()
  if (email === '') {
    errors.email = '请输入邮箱'
  } else if (!emailShapeOk(email)) {
    errors.email = '请输入有效的邮箱'
  }

  const issue = passwordIssue(draft.password, policy)
  if (issue !== null) {
    errors.password = issue
  } else if (draft.confirm === '') {
    errors.confirm = '请再次输入密码'
  } else if (draft.confirm !== draft.password) {
    errors.confirm = '两次输入的密码不一致'
  }

  if (!draft.agree) {
    errors.agree = '请先阅读并同意用户协议与隐私政策'
  }
  if (!status?.enabled) {
    errors.agree = '当前未开放自助注册'
  }

  return errors
}

/** 确认字段实时比对（旧线「字段级即时校验」口径）：非空且不一致即提示。 */
export function confirmLiveError(draft: RegisterDraft): string | undefined {
  return draft.confirm !== '' && draft.confirm !== draft.password ? '两次输入的密码不一致' : undefined
}

/** 只挑契约字段；确认字段与其他键都不外泄。 */
export function buildRegisterBody(
  draft: RegisterDraft,
  status: RegistrationStatus,
  turnstileToken?: string | null,
): RegisterRequest {
  const body: RegisterRequest = {
    email: draft.email.trim(),
    password: draft.password,
    agree_terms: draft.agree,
    agree_privacy: draft.agree,
    terms_version: status.terms_version,
    privacy_version: status.privacy_version,
  }
  const token = turnstileToken?.trim()
  if (token) body.turnstile_token = token
  return body
}

export type RegisterProblem = {
  /** 有字段落点时给字段；否则用通栏提示。 */
  field?: RegisterField
  message: string
}

/**
 * 服务端错误的字段落点。
 * 版本是否过期由 `current` 与提交时的版本比对：不一致才算过期。
 * 一致时的 409 归到邮箱重复。403 是开关被关；429 用 Retry-After。
 */
export function registerProblem(
  error: unknown,
  submitted: { termsVersion: number; privacyVersion: number },
  current: RegistrationStatus | null | undefined,
): RegisterProblem {
  if (error instanceof ApiError) {
    // 人机验证是独立结论：别让用户对着"参数不符合要求"猜哪里填错了。
    if (error.status === 400 && error.code === 'human_verification_failed') {
      return { message: '人机验证未通过，请重新完成验证' }
    }
    if (error.status === 403) {
      return { message: '当前未开放自助注册' }
    }
    if (error.status === 409) {
      const stale =
        current === null ||
        current === undefined ||
        current.terms_version !== submitted.termsVersion ||
        current.privacy_version !== submitted.privacyVersion
      return stale
        ? { message: '条款版本已更新，请重新阅读并勾选' }
        : { field: 'email', message: '该邮箱已注册' }
    }
    if (error.status === 429) {
      const wait =
        error.retryAfter === undefined ? '' : `，请 ${error.retryAfter} 秒后重试`
      return { message: `请求过于频繁${wait}` }
    }
    return { message: describeApiError(error) }
  }
  return { message: '网络异常，请稍后重试' }
}

/** 第一个出错字段；用于提交失败后的焦点落位。 */
export function firstRegisterField(errors: RegisterErrors): RegisterField | null {
  for (const field of ['email', 'password', 'confirm', 'agree'] as const) {
    if (errors[field]) return field
  }
  return null
}
