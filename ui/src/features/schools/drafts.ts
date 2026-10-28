/**
 * **功能**
 * 学校表单的纯逻辑：代码校验、请求体构造与变更差分。
 *
 * **归属**
 * `features/schools` 纯逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * code 的 pattern 与契约一致：`^[a-z0-9][a-z0-9-]{1,31}$`，创建后不可改。
 * code 不做大小写归一：大写是用户的输入错误，前端明确拒绝。
 * name 去首尾空格后必须 1..128 字。
 * 时区只有 `Asia/Shanghai` 一个可写值。
 * create 只带契约字段 code、name、timezone。
 * update 只带变更字段；无变更返回空对象，由组件禁用保存。
 * 校验不过时构造函数抛错。
 */

import type { School, SchoolCreate, SchoolStatus, SchoolUpdate } from '../../lib/api/school'

export const SCHOOL_CODE_PATTERN = /^[a-z0-9][a-z0-9-]{1,31}$/
export const SCHOOL_NAME_MAX = 128
export const SCHOOL_TIMEZONE = 'Asia/Shanghai' as const

export type SchoolTimezone = typeof SCHOOL_TIMEZONE

export type SchoolCreateDraft = {
  code: string
  name: string
  timezone: SchoolTimezone
}

export type SchoolUpdateDraft = {
  name: string
  status: SchoolStatus
  timezone: string
}

/** 创建时校验原始 code。首尾空格由 `buildSchoolCreate` 先去掉。 */
export function schoolCodeError(code: string): string | null {
  if (code === '') return '学校代码必填'
  if (!SCHOOL_CODE_PATTERN.test(code)) {
    return '代码需 2–32 位，小写字母、数字或连字符，且以字母或数字开头'
  }
  return null
}

/** 名称校验与表单共用一份：去首尾空格后 1..128 字。 */
export function schoolNameError(name: string): string | null {
  const trimmed = name.trim()
  if (trimmed === '') return '学校名称必填'
  if (trimmed.length > SCHOOL_NAME_MAX) return `学校名称最多 ${SCHOOL_NAME_MAX} 字`
  return null
}

function timezoneError(timezone: string): string | null {
  if (timezone !== SCHOOL_TIMEZONE) return '时区当前仅支持 Asia/Shanghai'
  return null
}

export function schoolCreateError(draft: SchoolCreateDraft): string | null {
  return (
    schoolCodeError(draft.code.trim()) ??
    schoolNameError(draft.name) ??
    timezoneError(draft.timezone)
  )
}

export function buildSchoolCreate(draft: SchoolCreateDraft): SchoolCreate {
  const error = schoolCreateError(draft)
  if (error !== null) {
    throw new Error(error)
  }
  return {
    code: draft.code.trim(),
    name: draft.name.trim(),
    timezone: draft.timezone,
  }
}

/**
 * 校验编辑草稿。时区只在它要改变时校验：
 * 历史学校若存了别的时区，名字与状态仍可改。
 */
export function schoolUpdateError(current: School, draft: SchoolUpdateDraft): string | null {
  const name = draft.name.trim()
  const nameIssue = schoolNameError(name)
  if (nameIssue !== null) return nameIssue
  if (draft.timezone !== current.timezone) return timezoneError(draft.timezone)
  return null
}

/** 只把真正变了的字段放进 PATCH 体。code 永不出现。 */
export function buildSchoolUpdate(current: School, draft: SchoolUpdateDraft): SchoolUpdate {
  const error = schoolUpdateError(current, draft)
  if (error !== null) {
    throw new Error(error)
  }
  const body: SchoolUpdate = {}
  const name = draft.name.trim()
  if (name !== current.name) body.name = name
  if (draft.status !== current.status) body.status = draft.status
  if (draft.timezone !== current.timezone) body.timezone = SCHOOL_TIMEZONE
  return body
}
