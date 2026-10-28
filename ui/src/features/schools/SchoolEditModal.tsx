/**
 * **功能**
 * 编辑学校模态：预填当前值，只发变更字段。
 *
 * **归属**
 * `features/schools` 展示层：差分在 drafts，发请求由调用方做。
 *
 * **口径**
 * code 创建后不可改，只读展示，永不进请求体。
 * 无变更时保存禁用，契约 `minProperties: 1` 在 UI 入口兑现。
 * 历史时区不在枚举内时，作为只读选项保留，避免下拉静默改写预填值。
 */

import { useState } from 'react'
import type { School, SchoolStatus, SchoolUpdate } from '@/lib/api/school'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { SelectInput, TextInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import { SCHOOL_STATUS_OPTIONS } from './display'
import {
  SCHOOL_TIMEZONE,
  buildSchoolUpdate,
  schoolNameError,
  schoolUpdateError,
  type SchoolUpdateDraft,
} from './drafts'

const EMPTY_DRAFT: SchoolUpdateDraft = {
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
  const [draft, setDraft] = useState<SchoolUpdateDraft>(() =>
    school ? { name: school.name, status: school.status, timezone: school.timezone } : EMPTY_DRAFT,
  )

  const validation = school ? schoolUpdateError(school, draft) : null
  const nameIssue = school ? schoolNameError(draft.name) : null
  const body = school && validation === null ? buildSchoolUpdate(school, draft) : {}
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
            onClick={() => onConfirm(body)}
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
          value={draft.name}
          onChange={(event) => setDraft((previous) => ({ ...previous, name: event.target.value }))}
        />
        <SelectInput
          id="school-edit-status"
          label="状态"
          options={SCHOOL_STATUS_OPTIONS}
          value={draft.status}
          onChange={(event) =>
            setDraft((previous) => ({
              ...previous,
              status: event.target.value as SchoolStatus,
            }))
          }
        />
        <SelectInput
          id="school-edit-timezone"
          label="时区"
          options={timezoneOptions}
          hint="可写值当前仅支持 Asia/Shanghai。"
          value={draft.timezone}
          onChange={(event) =>
            setDraft((previous) => ({ ...previous, timezone: event.target.value }))
          }
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
