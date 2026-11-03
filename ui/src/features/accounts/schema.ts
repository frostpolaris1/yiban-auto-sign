/**
 * **功能**
 * 账号表单的 zod schema：校验判据的单一事实源。
 *
 * **归属**
 * `features/accounts` 纯逻辑：`drafts.ts` 的校验谓词与本目录表单组件共用这里
 * 的一套判据（2026-10-29 天花板裁决 S2）；请求体构造仍在 `drafts.ts`。
 *
 * **口径**
 * 只校验交互必需（必填、长度、11 位手机号模式），不重复服务端语义裁决。
 * 手机号模式只属于编辑路径：建号前端今天只判必填，行为零变化（S2 冻结判据），
 * 模式裁决维持服务端。备注名按 Unicode 码点计长，与服务端 rune 计数同口径。
 * 更新草稿的键可缺省（drafts 的入参形状），缺省与空串同判。
 * 错误文案集中在此导出；模态级必填文案是历史单条文案，四类缺省共用。
 */

import { z } from 'zod'

/** 契约 pattern：11 位、1 开头。编辑路径专用，建号不做前端模式裁决。 */
export const PHONE_PATTERN = /^1[0-9]{10}$/

/** 备注名上限：按 Unicode 码点计，与服务端 rune 计数同口径。 */
export const NAME_MAX_CHARS = 50

export const PHONE_ERROR_TEXT = '手机号需 11 位、1 开头'
export const NAME_MAX_ERROR_TEXT = '备注名最多 50 字'
export const ACCOUNT_CREATE_REQUIRED_TEXT = '学校、手机号与易班口令都是必填'

/**
 * 建号表单（两步向导第 1 页与 master/admin 单页共用）。
 * 设备码与机型是自由文本，无前端判据；绑定用户的完整性与命中
 * 是异步预检（组件层防抖），不属于本 schema。
 */
export const accountCreateSchema = z.object({
  school_id: z.string().min(1, ACCOUNT_CREATE_REQUIRED_TEXT),
  phone: z.string().refine((value) => value.trim() !== '', ACCOUNT_CREATE_REQUIRED_TEXT),
  password: z.string().min(1, ACCOUNT_CREATE_REQUIRED_TEXT),
  name: z.string(),
  device_code: z.string().optional(),
  phone_model: z.string().optional(),
})

/**
 * 编辑草稿（PATCH）：留空＝不修改，故全部键可缺省。
 * phone 填了才校验模式；备注名按码点计长；密码不做任何判据（原样透传）。
 */
export const accountUpdateSchema = z.object({
  phone: z
    .string()
    .optional()
    .refine(
      (value) => value === undefined || value.trim() === '' || PHONE_PATTERN.test(value.trim()),
      PHONE_ERROR_TEXT,
    ),
  name: z
    .string()
    .optional()
    .refine(
      (value) => value === undefined || Array.from(value.trim()).length <= NAME_MAX_CHARS,
      NAME_MAX_ERROR_TEXT,
    ),
  password: z.string().optional(),
  device_code: z.string().optional(),
  phone_model: z.string().optional(),
  clear_device_code: z.boolean().optional(),
})

/** 建号表单的值形状（RHF 泛型用）。 */
export type AccountCreateFormValues = z.output<typeof accountCreateSchema>

/** 编辑草稿的值形状；与 `AccountUpdateDraft` 同构（drafts 定义请求体形状）。 */
export type AccountUpdateFormValues = z.input<typeof accountUpdateSchema>
