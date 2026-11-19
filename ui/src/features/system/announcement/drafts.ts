/**
 * **功能**
 * 全站公告区（#announcement）的纯校验逻辑。
 *
 * **归属**
 * `features/system/announcement` 纯逻辑层：对齐 `PUT /api/admin/announcement` 契约口径。
 *
 * **口径**
 * - 先去除首尾空白；若仍包含换行或行分隔符（`\n`、`\r`、`\u2028`、`\u2029`）则拦截。
 * - 按 Unicode 码点计数（`Array.from` 对齐后端 `utf8.RuneCountInString`），上限 200 字。
 * - 发布动作不接受空串（空串只能由「清空公告」显式触发 `{ text: '' }`），防止误清空线上公告。
 */

import { ApiError } from '@/lib/api/client'

export const ANNOUNCEMENT_MAX_CHARS = 200

// 对齐后端 announcementLineBreaks（env_io / Python str.splitlines 物理行分隔符全集）。
const LINE_BREAK_RE = /[\r\n\v\f\u001c-\u001e\u0085\u2028\u2029]/

export function countAnnouncementChars(raw: string): number {
  return Array.from(raw.trim()).length
}

export type AnnouncementValidationResult =
  | { valid: true; text: string }
  | { valid: false; error: string }

export function validateAnnouncementDraft(raw: string): AnnouncementValidationResult {
  const trimmed = raw.trim()
  if (trimmed.length === 0) {
    return {
      valid: false,
      error: '请输入公告内容；若要撤下当前公告，请点击「清空公告」',
    }
  }
  if (LINE_BREAK_RE.test(trimmed)) {
    return {
      valid: false,
      error: '公告仅支持单行文本，不能包含换行',
    }
  }
  if (Array.from(trimmed).length > ANNOUNCEMENT_MAX_CHARS) {
    return {
      valid: false,
      error: `公告最多 ${ANNOUNCEMENT_MAX_CHARS} 个字符，请精简后再试`,
    }
  }
  return { valid: true, text: trimmed }
}

/**
 * 公告接口的失败文案：已知状态给对应中文话术，其余状态兜底中文；
 * 后端 `code · message` 原文不外泄。
 */
export function formatAnnouncementError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return '登录已过期，请重新登录后再试'
    }
    if (error.status === 403) {
      return '仅主管理员可发布或清空公告'
    }
    if (error.status === 400) {
      return '公告内容格式未通过校验，请检查换行与字数后重试'
    }
    if (error.status === 429) {
      return '请求过于频繁，请稍后重试'
    }
    return '发布失败，请稍后重试'
  }
  return '保存公告时网络异常，请稍后重试'
}
