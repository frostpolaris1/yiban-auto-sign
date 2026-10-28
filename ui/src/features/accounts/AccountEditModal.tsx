/**
 * **功能**
 * 编辑账号模态：PATCH 四字段全 write-only，留空＝不修改。
 *
 * **归属**
 * `features/accounts` 展示层：草稿校验在 drafts，发请求由调用方做。
 *
 * **口径**
 * 空表单不可提交；phone 前端校验 11 位、1 开头；两个 64 上限由控件封顶。
 * school 字段不出现：P1 变更必 409，不做可点必败控件。
 */

import { useEffect, useState, type ChangeEvent } from 'react'
import type { Account } from '@/lib/api/account'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { TextInput } from '@/components/ui/Input'
import { accountUpdateError, type AccountUpdateDraft } from './drafts'

const EMPTY_DRAFT: Required<AccountUpdateDraft> = {
  phone: '',
  password: '',
  device_code: '',
  phone_model: '',
}

type AccountEditModalProps = {
  account: Account | null
  pending: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onConfirm: (draft: AccountUpdateDraft) => void
}

export function AccountEditModal({
  account,
  pending,
  error,
  returnFocusTo,
  onClose,
  onConfirm,
}: AccountEditModalProps) {
  const [draft, setDraft] = useState<AccountUpdateDraft>(EMPTY_DRAFT)
  const validation = accountUpdateError(draft)
  // 判定逐字段对齐纯函数口径：密码原样（含纯空格，契约 minLength 1 合法），
  // 其余字段与 buildAccountUpdate 同样 trim——空格占位不算填写。
  const hasAny =
    (draft.password !== undefined && draft.password !== '') ||
    [draft.phone, draft.device_code, draft.phone_model].some((v) => (v ?? '').trim() !== '')

  const field = (key: keyof AccountUpdateDraft) => ({
    value: draft[key] ?? '',
    onChange: (event: ChangeEvent<HTMLInputElement>) =>
      setDraft((previous) => ({ ...previous, [key]: event.target.value })),
  })

  useEffect(() => {
    if (account) setDraft(EMPTY_DRAFT)
  }, [account])

  return (
    <Modal
      open={account !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title="编辑账号？"
      description={account ? `账号 ${account.phone_masked}` : undefined}
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={pending || validation !== null || !hasAny}
            onClick={() => onConfirm(draft)}
          >
            保存修改
          </Button>
        </>
      }
    >
      <p className="app-muted">四个字段都只写不读：留空表示不修改。</p>
      <div className="app-form-stack">
        <TextInput
          id="account-edit-phone"
          label="手机号"
          placeholder="留空＝不修改"
          inputMode="numeric"
          error={validation ?? undefined}
          {...field('phone')}
        />
        <TextInput
          id="account-edit-password"
          label="密码"
          type="password"
          placeholder="留空＝不修改"
          {...field('password')}
        />
        <TextInput
          id="account-edit-device"
          label="设备识别码"
          placeholder="留空＝不修改"
          maxLength={64}
          {...field('device_code')}
        />
        <TextInput
          id="account-edit-model"
          label="机型"
          placeholder="留空＝不修改"
          maxLength={64}
          {...field('phone_model')}
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
