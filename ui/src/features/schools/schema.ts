/**
 * **功能**
 * 学校表单的 zod schema：校验判据的单一事实源。
 *
 * **归属**
 * `features/schools` 纯逻辑：`drafts.ts` 的校验谓词与本目录表单组件共用这里的
 * 一套判据（2026-10-29 天花板裁决 S2）；请求体构造与差分仍在 `drafts.ts`。
 *
 * **口径**
 * 只校验交互必需（必填、长度、pattern），不重复服务端语义裁决。
 * code 的 pattern 与契约一致；code 不做大小写归一，大写是输入错误。
 * 创建路径先 trim 再校验（与 buildSchoolCreate 同口径）；
 * `schoolCodeSchema` 校验原始值（历史谓词口径：空白即非法）。
 * name 去首尾空格后 1..128 字，按 UTF-16 码元计（与 drafts 现状一致）。
 * 时区只有 `Asia/Shanghai` 一个可写值；编辑时历史时区原值放行。
 */

import { z } from 'zod'

export const SCHOOL_CODE_PATTERN = /^[a-z0-9][a-z0-9-]{1,31}$/
export const SCHOOL_NAME_MAX = 128
export const SCHOOL_TIMEZONE = 'Asia/Shanghai' as const

export const SCHOOL_CODE_REQUIRED_TEXT = '学校代码必填'
export const SCHOOL_CODE_PATTERN_TEXT =
  '代码需 2–32 位，小写字母、数字或连字符，且以字母或数字开头'
export const SCHOOL_NAME_REQUIRED_TEXT = '学校名称必填'
export const SCHOOL_NAME_MAX_TEXT = `学校名称最多 ${SCHOOL_NAME_MAX} 字`
export const SCHOOL_TIMEZONE_TEXT = '时区当前仅支持 Asia/Shanghai'

/** code 原始值判据：`schoolCodeError` 的历史口径，调用方自行决定是否先 trim。 */
export const schoolCodeSchema = z
  .string()
  .min(1, SCHOOL_CODE_REQUIRED_TEXT)
  .regex(SCHOOL_CODE_PATTERN, SCHOOL_CODE_PATTERN_TEXT)

/** 名称判据：trim 后必填、不超过上限；两条信息按顺序短路。 */
export const schoolNameSchema = z
  .string()
  .refine((value) => value.trim() !== '', SCHOOL_NAME_REQUIRED_TEXT)
  .refine((value) => value.trim().length <= SCHOOL_NAME_MAX, SCHOOL_NAME_MAX_TEXT)

/** 时区判据：唯一可写值（字面量，创建表单的值类型随之收窄）。 */
export const schoolTimezoneSchema = z.literal(SCHOOL_TIMEZONE, SCHOOL_TIMEZONE_TEXT)

/** 创建：code 先 trim 再判（与 buildSchoolCreate 同口径）。 */
export const schoolCreateSchema = z.object({
  code: z
    .string()
    .refine((value) => value.trim() !== '', SCHOOL_CODE_REQUIRED_TEXT)
    .refine((value) => SCHOOL_CODE_PATTERN.test(value.trim()), SCHOOL_CODE_PATTERN_TEXT),
  name: schoolNameSchema,
  timezone: schoolTimezoneSchema,
})

/**
 * 编辑：时区只在它要改变时校验——历史学校若存了别的时区，
 * 原值放行，名字与状态仍可改。status 由下拉约束，不设前端判据。
 */
export function schoolUpdateSchema(current: { timezone: string }) {
  return z.object({
    name: schoolNameSchema,
    status: z.string(),
    timezone: z.string().refine(
      (value) => value === SCHOOL_TIMEZONE || value === current.timezone,
      SCHOOL_TIMEZONE_TEXT,
    ),
  })
}

/** 创建表单的值形状（RHF 泛型用）。 */
export type SchoolCreateFormValues = z.output<typeof schoolCreateSchema>

/** 编辑表单的值形状（RHF 泛型用）。 */
export type SchoolUpdateFormValues = z.output<ReturnType<typeof schoolUpdateSchema>>
