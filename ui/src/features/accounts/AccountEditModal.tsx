/**
 * **功能**
 * 编辑账号模态：PATCH 四字段全 write-only，留空＝不修改。
 *
 * **归属**
 * `features/accounts` 展示层：表单状态与校验走 react-hook-form＋zod
 * （2026-10-29 天花板裁决 S2），判据单一来源在 `schema.ts`；
 * 请求体构造在 `drafts.ts`（buildAccountUpdate），发请求由调用方做。
 *
 * **口径**
 * 空表单不可提交；phone 前端校验 11 位、1 开头；两个 64 上限由控件封顶。
 * 校验实时（mode: onChange），与迁移前的逐渲染判据一致；
 * 校验文案沿用历史位置：统一显示在手机号字段下。
 * 备注名是唯一回填字段：与打开时相同则不发，清空即删除备注。
 * school 字段不出现：P1 变更必 409，不做可点必败控件。
 * 设备识别码只对 master/admin 显示：user 的设备码只能由平台回读，
 * 这里不给必败控件。机型是非凭据标签，三角色都可改。
 * 清除设备识别码是唯一发送空串的路径；勾选后输入禁用，取消即恢复。
 * 被拒账号的入口与标题写「修改并重新提交」，模态里展示驳回理由。
 * 只改备注名不重提；改凭据或改绑手机号才回待审核，模态文案先说清。
 */

import { useEffect } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import type { Account } from '@/lib/api/account'
import type { Role } from '@/lib/api/auth'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { TextInput } from '@/components/ui/Input'
import { accountUpdateSchema, type AccountUpdateFormValues } from './schema'
import type { AccountUpdateDraft } from './drafts'

const EMPTY_DRAFT: Required<AccountUpdateDraft> = {
  phone: '',
  name: '',
  password: '',
  device_code: '',
  phone_model: '',
  clear_device_code: false,
}

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
  const { register, watch, getValues, reset, handleSubmit, formState } =
    useForm<AccountUpdateFormValues>({
      resolver: zodResolver(accountUpdateSchema),
      // 实时校验与迁移前一致：判据逐渲染生效，不等首次提交。
      mode: 'onChange',
      // 与现状一致：不把焦点搬去字段。
      shouldFocusError: false,
      defaultValues: EMPTY_DRAFT,
    })
  const values = watch()
  const resubmit = account?.status === 'rejected'
  // 设备码归平台设备身份：只有管理者能手填或清除。
  const canManageDevice = role === 'master' || role === 'admin'
  const clearingDevice = values.clear_device_code === true
  // 备注名回填后与打开时的原值比较：没变不算一次编辑。
  const nameChanged = normalizeName(values.name) !== normalizeName(account?.name)
  // 判定逐字段对齐 buildAccountUpdate 口径：密码原样（含纯空格，契约
  // minLength 1 合法），其余字段同样 trim——空格占位不算填写。
  const hasAny =
    nameChanged ||
    (values.password !== undefined && values.password !== '') ||
    clearingDevice ||
    [values.phone, values.device_code, values.phone_model].some((v) => (v ?? '').trim() !== '')

  // 校验入口在 zod（schema.ts）。历史 UX：校验文案统一挂在手机号字段下。
  const validation = formState.errors.phone?.message ?? formState.errors.name?.message ?? null

  useEffect(() => {
    if (account) reset({ ...EMPTY_DRAFT, name: account.name })
  }, [account, reset])

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
            onClick={handleSubmit(() => onConfirm(getValues()))}
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
          {...register('name')}
        />
        <TextInput
          id="account-edit-phone"
          label="手机号"
          placeholder="留空＝不修改"
          inputMode="numeric"
          error={validation ?? undefined}
          {...register('phone')}
        />
        <TextInput
          id="account-edit-password"
          label="密码"
          type="password"
          placeholder="留空＝不修改"
          {...register('password')}
        />
        {canManageDevice ? (
          <>
            <TextInput
              id="account-edit-device"
              label="设备识别码"
              placeholder={clearingDevice ? '保存后清除' : '留空＝不修改'}
              maxLength={64}
              disabled={clearingDevice}
              {...register('device_code')}
            />
            <label className="app-filter-check">
              <input type="checkbox" {...register('clear_device_code')} />
              清除设备识别码
            </label>
          </>
        ) : null}
        <TextInput
          id="account-edit-model"
          label="机型"
          placeholder="留空＝不修改"
          maxLength={64}
          {...register('phone_model')}
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
