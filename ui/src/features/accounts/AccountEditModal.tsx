/**
 * **功能**
 * 编辑账号模态：PATCH 四字段全 write-only，留空＝不修改。
 *
 * **归属**
 * `features/accounts` 展示层：草稿校验在 drafts，发请求由调用方做。
 *
 * **口径**
 * 空表单不可提交；phone 前端校验 11 位、1 开头；两个 64 上限由控件封顶。
 * 备注名是唯一回填字段：与打开时相同则不发，清空即删除备注。
 * school 字段不出现：P1 变更必 409，不做可点必败控件。
 * 设备识别码只对 master/admin 显示：user 的设备码只能由平台回读，
 * 这里不给必败控件。机型是非凭据标签，三角色都可改。
 * 清除设备识别码是唯一发送空串的路径；勾选后输入禁用，取消即恢复。
 * 被拒账号的入口与标题写「修改并重新提交」，模态里展示驳回理由。
 * 只改备注名不重提；改凭据或改绑手机号才回待审核，模态文案先说清。
 */

import { useEffect, useState, type ChangeEvent } from 'react'
import type { Account } from '@/lib/api/account'
import type { Role } from '@/lib/api/auth'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { TextInput } from '@/components/ui/Input'
import { accountUpdateError, type AccountUpdateDraft } from './drafts'

const EMPTY_DRAFT: Required<AccountUpdateDraft> = {
  phone: '',
  name: '',
  password: '',
  device_code: '',
  phone_model: '',
  clear_device_code: false,
}

/** 草稿里可直接绑到文本控件的键；清除开关是布尔值，单独处理。 */
type TextFieldKey = 'phone' | 'name' | 'password' | 'device_code' | 'phone_model'

/** 备注名按码点比较，与服务端 trim + rune 计长同口径。 */
function normalizeName(value: string | undefined): string {
  return (value ?? '').trim()
}

type AccountEditModalProps = {
  account: Account | null
  role: Role | null
  pending: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onConfirm: (draft: AccountUpdateDraft) => void
}

export function AccountEditModal({
  account,
  role,
  pending,
  error,
  returnFocusTo,
  onClose,
  onConfirm,
}: AccountEditModalProps) {
  const [draft, setDraft] = useState<AccountUpdateDraft>(EMPTY_DRAFT)
  const validation = accountUpdateError(draft)
  const resubmit = account?.status === 'rejected'
  // 设备码归平台设备身份：只有管理者能手填或清除。
  const canManageDevice = role === 'master' || role === 'admin'
  const clearingDevice = draft.clear_device_code === true
  // 备注名回填后与打开时的原值比较：没变不算一次编辑。
  const nameChanged = normalizeName(draft.name) !== normalizeName(account?.name)
  // 判定逐字段对齐纯函数口径：密码原样（含纯空格，契约 minLength 1 合法），
  // 其余字段与 buildAccountUpdate 同样 trim——空格占位不算填写。
  const hasAny =
    nameChanged ||
    (draft.password !== undefined && draft.password !== '') ||
    clearingDevice ||
    [draft.phone, draft.device_code, draft.phone_model].some((v) => (v ?? '').trim() !== '')

  const field = (key: TextFieldKey) => ({
    value: draft[key] ?? '',
    onChange: (event: ChangeEvent<HTMLInputElement>) =>
      setDraft((previous) => ({ ...previous, [key]: event.target.value })),
  })

  useEffect(() => {
    if (account) setDraft({ ...EMPTY_DRAFT, name: account.name })
  }, [account])

  return (
    <Modal
      open={account !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title={resubmit ? '修改并重新提交？' : '编辑账号？'}
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
      {resubmit ? (
        <Alert tone="warning" title="已驳回">
          驳回理由：{account?.reject_reason || '未填写'}。修改凭据后重新提交；
          只改备注名不会重提，账号保持已驳回。
        </Alert>
      ) : null}
      <p className="app-muted">改绑手机号会回到待审核：审核结论绑定旧号。</p>
      <p className="app-muted">
        除备注名外，各字段只写不读：留空表示不修改。备注名留空时显示为「账号+手机尾号」。
      </p>
      <div className="app-form-stack">
        <TextInput
          id="account-edit-name"
          label="备注名"
          placeholder="留空＝使用默认名"
          maxLength={50}
          {...field('name')}
        />
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
        {canManageDevice ? (
          <>
            <TextInput
              id="account-edit-device"
              label="设备识别码"
              placeholder={clearingDevice ? '保存后清除' : '留空＝不修改'}
              maxLength={64}
              disabled={clearingDevice}
              {...field('device_code')}
            />
            <label className="app-filter-check">
              <input
                type="checkbox"
                checked={clearingDevice}
                onChange={(event) =>
                  setDraft((previous) => ({
                    ...previous,
                    clear_device_code: event.target.checked,
                  }))
                }
              />
              清除设备识别码
            </label>
          </>
        ) : null}
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
