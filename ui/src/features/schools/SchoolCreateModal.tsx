/**
 * **功能**
 * 新建学校模态：code、name、timezone 三个契约字段。
 *
 * **归属**
 * `features/schools` 展示层：表单状态与校验走 react-hook-form＋zod
 * （2026-10-29 天花板裁决 S2），判据单一来源在 `schema.ts`；
 * 请求体构造在 `drafts.ts`（buildSchoolCreate），发请求由调用方做。
 *
 * **口径**
 * code 非法时保存禁用，并显示契约 pattern 的原因；空输入不报错，只显示提示。
 * name 上限 128 由控件封顶。
 * 时区只有 `Asia/Shanghai` 一个选项。
 * 打开即重置草稿（调用方按开合换 key），避免上一次的输入残留。
 */

import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { SelectInput, TextInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import { SCHOOL_TIMEZONE, type SchoolCreateDraft } from './drafts'
import { schoolCreateSchema, type SchoolCreateFormValues } from './schema'

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
  const { register, watch, handleSubmit, formState } = useForm<SchoolCreateFormValues>({
    resolver: zodResolver(schoolCreateSchema),
    // 实时校验与迁移前一致：判据逐渲染生效，不等首次提交。
    mode: 'onChange',
    // 与现状一致：不把焦点搬去字段。
    shouldFocusError: false,
    defaultValues: EMPTY_DRAFT,
  })
  const codeValue = watch('code')
  // 空输入不报错，只显示提示；填了非法值才显示原因。
  const codeError =
    codeValue.trim() === '' ? undefined : (formState.errors.code?.message ?? undefined)

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
            disabled={pending || !formState.isValid}
            onClick={handleSubmit((values) => onConfirm(values))}
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
          {...register('code')}
        />
        <TextInput
          id="school-create-name"
          label="学校名称"
          required
          maxLength={128}
          placeholder="合成学校名称"
          {...register('name')}
        />
        <SelectInput
          id="school-create-timezone"
          label="时区"
          options={TIMEZONE_OPTIONS}
          hint="当前仅支持 Asia/Shanghai。"
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
