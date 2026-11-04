/**
 * **功能**
 * 编辑学校模态：预填当前值，只发变更字段。
 *
 * **归属**
 * `features/schools` 展示层：表单状态与校验走 react-hook-form＋zod
 * （2026-10-29 天花板裁决 S2），判据单一来源在 `schema.ts`；
 * 差分在 `drafts.ts`（buildSchoolUpdate），发请求由调用方做。
 *
 * **口径**
 * code 创建后不可改，只读展示，永不进请求体。
 * 无变更时保存禁用，契约 `minProperties: 1` 在 UI 入口兑现。
 * 历史时区不在枚举内时，作为只读选项保留，避免下拉静默改写预填值。
 */

import { useMemo } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import type { School, SchoolStatus, SchoolUpdate } from '@/lib/api/school'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { SelectInput, TextInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import { SCHOOL_STATUS_OPTIONS } from './display'
import { SCHOOL_TIMEZONE, buildSchoolUpdate, type SchoolUpdateDraft } from './drafts'
import { schoolUpdateSchema, type SchoolUpdateFormValues } from './schema'

const EMPTY_DRAFT: SchoolUpdateFormValues = {
  name: '',
  status: 'active',
  timezone: SCHOOL_TIMEZONE,
}

type SchoolEditModalProps = {
  school: School | null
  pending: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onConfirm: (body: SchoolUpdate) => void
}

export function SchoolEditModal({
  school,
  pending,
  error,
  returnFocusTo,
  onClose,
  onConfirm,
}: SchoolEditModalProps) {
  // 调用方按学校 ID 换 key：每次打开都从当前值重新预填，不残留上一次草稿。
  // resolver 随 current.timezone 记忆化：历史时区原值放行由 schema 表达。
  const schema = useMemo(
    () => schoolUpdateSchema(school ?? { timezone: SCHOOL_TIMEZONE }),
    [school],
  )
  const { register, watch, handleSubmit } = useForm<SchoolUpdateFormValues>({
    resolver: zodResolver(schema),
    // 实时校验与迁移前一致：判据逐渲染生效，不等首次提交。
    mode: 'onChange',
    // 与现状一致：不把焦点搬去字段。
    shouldFocusError: false,
    defaultValues: school
      ? { name: school.name, status: school.status, timezone: school.timezone }
      : EMPTY_DRAFT,
  })
  const values = watch()

  // 校验入口在 zod（schema.ts）：名字 + 「时区要改才是合法值」。
  // 门与展示用同步 safeParse：resolver 是异步的，渲染里若只读
  // formState.errors 会慢一拍，差分会在校验落地前跑到 buildSchoolUpdate。
  const parsed = schema.safeParse(values)
  const validation = parsed.success ? null : (parsed.error.issues[0]?.message ?? null)
  const nameIssue =
    parsed.success ? null : (parsed.error.issues.find((issue) => issue.path[0] === 'name')?.message ?? null)
  const body =
    school && validation === null
      ? buildSchoolUpdate(school, {
          name: values.name,
          status: values.status as SchoolStatus,
          timezone: values.timezone,
        } satisfies SchoolUpdateDraft)
      : {}
  const changed = Object.keys(body).length > 0

  const timezoneOptions =
    school && school.timezone !== SCHOOL_TIMEZONE
      ? [
          { value: school.timezone, label: school.timezone },
          { value: SCHOOL_TIMEZONE, label: SCHOOL_TIMEZONE },
        ]
      : [{ value: SCHOOL_TIMEZONE, label: SCHOOL_TIMEZONE }]

  return (
    <Modal
      open={school !== null}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title="编辑学校？"
      description={school ? `学校 ${school.name}` : undefined}
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={pending || !changed}
            onClick={handleSubmit(() => onConfirm(body))}
          >
            保存修改
          </Button>
        </>
      }
    >
      {school ? (
        <p className="app-muted">
          学校代码 <span className="data-cell-mono">{school.code}</span>（创建后不可修改）
        </p>
      ) : null}
      <div className="app-form-stack">
        <TextInput
          id="school-edit-name"
          label="学校名称"
          required
          maxLength={128}
          error={nameIssue ?? undefined}
          {...register('name')}
        />
        <SelectInput
          id="school-edit-status"
          label="状态"
          options={SCHOOL_STATUS_OPTIONS}
          {...register('status')}
        />
        <SelectInput
          id="school-edit-timezone"
          label="时区"
          options={timezoneOptions}
          hint="可写值当前仅支持 Asia/Shanghai。"
          {...register('timezone')}
        />
      </div>
      {error ? (
        <div className="app-modal-error">
          <Alert tone="danger" title="请求失败">
            {error}
          </Alert>
        </div>
      ) : null}
    </Modal>
  )
}
