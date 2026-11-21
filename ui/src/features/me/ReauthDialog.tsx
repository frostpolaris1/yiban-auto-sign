/**
 * **功能**
 * 危险操作的二次口令弹层：删除、恢复、设备刷新、凭据编辑共用。
 *
 * **归属**
 * `features/me` 展示层：只收集本站登录口令，不自己发请求。
 *
 * **口径**
 * 口令只存在于组件内，关闭即清空；不写日志、不进 URL。
 * 400 缺口令、403 口令错、429 连续失败由调用方映射成可见文案。
 * 确认中禁用按钮，避免重复提交。
 */

import { useEffect, useState } from 'react'
import { Alert } from '@/components/ui/Alert'
import { PasswordInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'

type ReauthDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel: string
  pending: boolean
  error: string | null
  onConfirm: (password: string) => void
  /** 打开弹层的控件；关闭时焦点回到它。 */
  returnFocusTo?: HTMLElement | null
  /** 破坏性操作确认键用红色实心，权重高于普通保存。 */
  danger?: boolean
}

export function ReauthDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  pending,
  error,
  onConfirm,
  returnFocusTo,
  danger = false,
}: ReauthDialogProps) {
  const [password, setPassword] = useState('')

  useEffect(() => {
    if (!open) setPassword('')
  }, [open])

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      returnFocusTo={returnFocusTo}
      description={description}
      footer={
        <>
          <button
            type="button"
            className="uapp-button uapp-button--ghost"
            disabled={pending}
            onClick={() => onOpenChange(false)}
          >
            取消
          </button>
          <button
            type="submit"
            form="uapp-reauth-form"
            className={danger ? 'uapp-button uapp-button--danger' : 'uapp-button uapp-button--primary'}
            disabled={pending || password.trim() === ''}
          >
            {pending ? '处理中…' : confirmLabel}
          </button>
        </>
      }
    >
      <form
        id="uapp-reauth-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          if (pending || password.trim() === '') return
          onConfirm(password)
        }}
      >
        <PasswordInput
          id="uapp-reauth-password"
          label="本站登录口令"
          name="current_password"
          autoComplete="current-password"
          placeholder="输入你登录本站用的口令"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        {error ? (
          <div className="uapp-alert-block">
            <Alert tone="danger">{error}</Alert>
          </div>
        ) : null}
      </form>
    </Modal>
  )
}
