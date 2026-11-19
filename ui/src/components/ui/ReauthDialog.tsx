/**
 * **功能**
 * 危险操作的二次口令弹窗：收集操作者本人的本站登录口令。
 *
 * **归属**
 * `components` 通用件：只收集口令与展示失败，发请求由调用方做。
 *
 * **口径**
 * 骨架照用户端 App 的 `ReauthDialog`（只读参考）：一个口令框、
 * 一个取消、一个确认；确认在途时禁用。口令只存在于组件内，
 * 关闭即清空；不写日志、不进 URL、不进任何持久层。
 * 400 缺口令、403 口令错、429 连续失败由调用方映射成可见文案；
 * 本组件只保证失败时弹窗不自动关闭。
 * `layer` 透传给 `Modal`：叠在抽屉或确认弹窗之上时给更大的层号。
 */

import { useEffect, useId, useState, type ReactNode } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Button, type ButtonVariant } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { PasswordInput } from '@/components/ui/Input'

type ReauthDialogProps = {
  open: boolean
  title: string
  description?: ReactNode
  confirmText: string
  pending?: boolean
  error?: string | null
  /** 破坏性操作确认键用红色实心。 */
  danger?: boolean
  returnFocusTo?: HTMLElement | null
  /** 叠层序号，透传给 `Modal`；缺省 1。 */
  layer?: number
  onConfirm: (password: string) => void
  onClose: () => void
  children?: ReactNode
}

export function ReauthDialog({
  open,
  title,
  description,
  confirmText,
  pending = false,
  error,
  danger = false,
  returnFocusTo,
  layer,
  onConfirm,
  onClose,
  children,
}: ReauthDialogProps) {
  const [password, setPassword] = useState('')
  const formId = useId()
  const inputId = `reauth-password-${formId.replace(/[^a-zA-Z0-9_-]/g, '')}`

  // 关闭即清空：口令不得留在下一次打开的状态里。
  useEffect(() => {
    if (!open) setPassword('')
  }, [open])

  const variant: ButtonVariant = danger ? 'danger' : 'primary'

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title={title}
      description={description}
      returnFocusTo={returnFocusTo}
      layer={layer}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            取消
          </Button>
          <Button
            type="submit"
            form={formId}
            variant={variant}
            disabled={pending || password.trim() === ''}
          >
            {pending ? '处理中…' : confirmText}
          </Button>
        </>
      }
    >
      {children}
      <form
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          if (pending || password.trim() === '') return
          onConfirm(password)
        }}
      >
        <div className="app-form-stack">
          <PasswordInput
            id={inputId}
            label="你的登录口令"
            hint="用于本次二次确认，不会保存或出现在任何记录里。"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
      </form>
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
