/**
 * **功能**
 * 确认弹窗：一句话说明后果、一个取消、一个确认。
 *
 * **归属**
 * `components` 通用件：只排版，动作由调用方给。
 *
 * **口径**
 * 确认前调用方不得发请求：本组件只在确认按钮点击时回调。
 * 失败文案显示在弹窗内，弹窗不自动关闭。
 * `layer` 透传给 `Modal`：叠在另一个弹窗之上时给更大的层号。
 */

import { useEffect, useId, useState, type ReactNode } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { PasswordInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'

type ConfirmModalProps = {
  open: boolean
  title: string
  description?: ReactNode
  confirmText: string
  pending?: boolean
  error?: string | null
  tone?: 'primary' | 'danger'
  returnFocusTo?: HTMLElement | null
  /** 叠层序号，透传给 `Modal`；缺省 1。 */
  layer?: number
  /**
   * 危险操作二次口令：收集操作者本人本站登录口令并随确认回调传出。
   * 口令只存组件内、关闭即清；不写日志、不进 URL。缺省关闭保持旧调用不变。
   */
  requirePassword?: boolean
  onConfirm: (password?: string) => void
  onClose: () => void
  children?: ReactNode
}

export function ConfirmModal({
  open,
  title,
  description,
  confirmText,
  pending,
  error,
  tone = 'primary',
  returnFocusTo,
  layer,
  requirePassword = false,
  onConfirm,
  onClose,
  children,
}: ConfirmModalProps) {
  const passwordId = useId()
  const [password, setPassword] = useState('')
  useEffect(() => {
    if (!open) setPassword('')
  }, [open])
  const blocked = pending || (requirePassword && password.trim() === '')
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
            variant={tone}
            onClick={() => onConfirm(requirePassword ? password : undefined)}
            disabled={blocked}
          >
            {confirmText}
          </Button>
        </>
      }
    >
      {requirePassword ? (
        <PasswordInput
          id={passwordId}
          label="操作者本站口令"
          name="current_password"
          autoComplete="current-password"
          placeholder="输入你登录本站用的口令"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      ) : null}
      {children}
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
