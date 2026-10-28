/**
 * **功能**
 * 新建学校模态：code、name、timezone 三个契约字段。
 *
 * **归属**
 * `features/schools` 展示层：校验在 drafts，发请求由调用方做。
 *
 * **口径**
 * code 非法时保存禁用，并显示契约 pattern 的原因。
 * name 上限 128 由控件封顶。
 * 时区只有 `Asia/Shanghai` 一个选项。
 * 打开即重置草稿，避免上一次的输入残留。
 */

import { useState, type ChangeEvent } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { SelectInput, TextInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import {
  SCHOOL_TIMEZONE,
  schoolCodeError,
  schoolCreateError,
  type SchoolCreateDraft,
} from './drafts'

const EMPTY_DRAFT: SchoolCreateDraft = {
  code: '',
  name: '',
  timezone: SCHOOL_TIMEZONE,
}

const TIMEZONE_OPTIONS = [{ value: SCHOOL_TIMEZONE, label: SCHOOL_TIMEZONE }]

type SchoolCreateModalProps = {
  open: boolean
  pending: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onConfirm: (draft: SchoolCreateDraft) => void
}

export function SchoolCreateModal({
  open,
  pending,
  error,
  returnFocusTo,
  onClose,
  onConfirm,
}: SchoolCreateModalProps) {
  // 调用方按开合换 key：每次打开都是空表单。
  const [draft, setDraft] = useState<SchoolCreateDraft>(EMPTY_DRAFT)
  const validation = schoolCreateError(draft)
  // 空输入不报错，只显示提示；填了非法值才显示原因。
  const codeError = draft.code.trim() === '' ? undefined : (schoolCodeError(draft.code.trim()) ?? undefined)

  const field = (key: 'code' | 'name') => ({
    value: draft[key],
    onChange: (event: ChangeEvent<HTMLInputElement>) =>
      setDraft((previous) => ({ ...previous, [key]: event.target.value })),
  })

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title="新建学校？"
      description="新学校进入正常状态。代码创建后不可修改。"
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={pending || validation !== null}
            onClick={() => onConfirm(draft)}
          >
            创建学校
          </Button>
        </>
      }
    >
      <div className="app-form-stack">
        <TextInput
          id="school-create-code"
          label="学校代码"
          required
          placeholder="例如 syn-school-01"
          hint="2–32 位，小写字母、数字或连字符，且以字母或数字开头。"
          error={codeError}
          {...field('code')}
        />
        <TextInput
          id="school-create-name"
          label="学校名称"
          required
          maxLength={128}
          placeholder="合成学校名称"
          {...field('name')}
        />
        <SelectInput
          id="school-create-timezone"
          label="时区"
          options={TIMEZONE_OPTIONS}
          hint="当前仅支持 Asia/Shanghai。"
          value={draft.timezone}
          // 单选项：控件只提供契约里唯一可写值。
          onChange={() => setDraft((previous) => ({ ...previous, timezone: SCHOOL_TIMEZONE }))}
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
