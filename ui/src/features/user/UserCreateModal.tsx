/**
 * **功能**
 * 新建用户弹窗：邮箱、口令、角色三字段与提交。
 *
 * **归属**
 * `features/user` 展示层：建号走 `POST /api/users`，成功回调容器。
 *
 * **口径**
 * 只有 master 能进本页，故弹窗不再判角色。
 * 三个字段都用 `FilterField` 壳：标签关联与错误位由壳负责。
 * 提交在途时按钮禁用（BusyButton），防重复建号。
 * 409 是「邮箱已被活跃用户使用」：错误留在弹窗内，输入不丢。
 * 其余错误同样留在弹窗内，可回看后重试。
 * 成功即关弹窗：口令不驻留任何状态。
 */

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { FilterField } from '@/components/ui/FilterField'
import { Modal } from '@/components/ui/Modal'
import { describeApiError } from '@/lib/api/client'
import { createUser, type User, type UserRole } from '@/lib/api/user'
import { USER_ROLE_OPTIONS } from './display'
import { isConflict } from './apiErrors'

const TAKEN_EMAIL_TEXT = '该邮箱已被使用'

type UserCreateModalProps = {
  open: boolean
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onCreated: (user: User) => void
}

export function UserCreateModal({ open, returnFocusTo, onClose, onCreated }: UserCreateModalProps) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<UserRole>('user')
  const [error, setError] = useState<string | null>(null)

  const createMut = useMutation({
    mutationFn: () => createUser({ email: email.trim(), password, role }),
    onSuccess: (user) => {
      // 口令不驻留：立即清空三个字段再交给容器。
      setEmail('')
      setPassword('')
      setRole('user')
      setError(null)
      onCreated(user)
    },
    onError: (cause) => {
      setError(isConflict(cause) ? TAKEN_EMAIL_TEXT : describeApiError(cause))
    },
  })

  const submit = () => {
    if (!email.trim() || !password) {
      setError('邮箱与口令都是必填')
      return
    }
    setError(null)
    createMut.mutate()
  }

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title="新建用户"
      description="邮箱是登录名；角色决定可见面。"
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={createMut.isPending}>
            取消
          </Button>
          <BusyButton
            variant="primary"
            busy={createMut.isPending}
            busyLabel="创建中…"
            onClick={submit}
          >
            创建
          </BusyButton>
        </>
      }
    >
      <div className="app-form-stack">
        <FilterField id="user-create-email" label="邮箱" required>
          <input
            className="input"
            type="email"
            autoComplete="off"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </FilterField>

        <FilterField id="user-create-password" label="初始口令" required>
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </FilterField>

        <FilterField id="user-create-role" label="角色" required>
          <select
            className="select"
            value={role}
            onChange={(event) => setRole(event.target.value as UserRole)}
          >
            {USER_ROLE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FilterField>
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
