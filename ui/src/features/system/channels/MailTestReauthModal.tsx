/**
 * **功能**
 * 测试邮件触发时的二次密码复核弹窗（不直接导入其他特性切片）。
 *
 * **归属**
 * `features/system/channels` 子组件：遵循 `features/me/ReauthDialog` 的 Adminator 交互骨架。
 *
 * **口径**
 * - 打开时聚焦密码输入，关闭或成功后清空密码与弹内错误。
 * - `400` 缺口令、`403` 口令错误及复核节流 `429` 留在弹窗内提示；其余状态关闭弹窗并在按钮旁渲染。
 */

import { useEffect, useState, type FormEvent } from 'react'
import { Alert } from '@/components/ui/Alert'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { PasswordInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'

type MailTestReauthModalProps = {
  open: boolean
  busy: boolean
  error: string | null
  triggerEl: HTMLElement | null
  onClearError: () => void
  onClose: () => void
  onSubmit: (currentPassword: string) => void
}

export function MailTestReauthModal({
  open,
  busy,
  error,
  triggerEl,
  onClearError,
  onClose,
  onSubmit,
}: MailTestReauthModalProps) {
  const [password, setPassword] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) {
      setPassword('')
      setLocalError(null)
    }
  }, [open])

  useEffect(() => {
    if (open && error) {
      document.getElementById('mail-test-current-password')?.focus()
    }
  }, [open, error])

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    onClearError()
    if (password.length === 0) {
      setLocalError('请输入当前登录密码')
      document.getElementById('mail-test-current-password')?.focus()
      return
    }
    setLocalError(null)
    onSubmit(password)
  }

  return (
    <Modal
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen && !busy) onClose()
      }}
      title="确认发送测试邮件"
      returnFocusTo={triggerEl}
    >
      <form className="app-form-stack" onSubmit={handleSubmit} noValidate>
        <p className="sys-policy-desc">
          将向当前登录主管理员邮箱发送测试邮件；请输入当前登录密码完成危险操作复核。
        </p>

        <PasswordInput
          id="mail-test-current-password"
          name="current_password"
          label="当前登录密码"
          autoComplete="current-password"
          autoFocus
          required
          disabled={busy}
          value={password}
          error={localError ?? undefined}
          onChange={(e) => {
            setLocalError(null)
            onClearError()
            setPassword(e.target.value)
          }}
        />

        {error ? (
          <div className="app-modal-error">
            <Alert tone="danger">{error}</Alert>
          </div>
        ) : null}

        <div className="sys-modal-footer">
          <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <BusyButton
            type="submit"
            variant="primary"
            busy={busy}
            busyLabel="验证并发送…"
          >
            确认发送
          </BusyButton>
        </div>
      </form>
    </Modal>
  )
}
