/**
 * **功能**
 * 用户管理抽屉：摘要、改角色、停用/启用、重置口令、软删与恢复。
 *
 * **归属**
 * `features/user` 展示层：写操作走 react-query，状态留在容器。
 *
 * **口径**
 * 打开即用列表摘要渲染；每个动作的失败留在原控件旁，可回看。
 * 409 是业务结论不是故障：最后一名 master 降级/停用给固定文案，
 * 名下有账号给「先转移或删号」。其余错误走统一错误文本。
 * temp_password 是单次可见的秘密：只在本次渲染出现，关闭即弃；
 * 组件卸载与「我已保存」都会清掉它，不再出现在任何状态里。
 * 删除成功后行将消失：容器负责收抽屉并失效列表。
 * 恢复按钮只对已删行出现；恢复后角色与状态保持原值。
 */

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { ConfirmModal } from '@/components/ui/ConfirmModal'
import { Drawer } from '@/components/ui/Drawer'
import { notifySuccess } from '@/components/ui/toast'
import { describeApiError } from '@/lib/api/client'
import {
  deleteUser,
  resetUserPassword,
  restoreUser,
  updateUser,
  type User,
  type UserRole,
} from '@/lib/api/user'
import { formatDateTime } from '@/lib/format'
import { roleLabel } from '@/lib/identity'
import { USER_ROLE_OPTIONS } from './display'
import { isConflict } from './apiErrors'

const LAST_MASTER_TEXT = '这是最后一位管理员，不能降级或停用'
const OWNED_ACCOUNTS_TEXT = '该用户名下还有账号，先转移或删除账号'

type UserDetailDrawerProps = {
  user: User | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  /** 动作成功后同步最新行：容器更新抽屉并失效列表。 */
  onSync: (user: User) => void
  /** 行已消失（软删成功）：容器收抽屉并失效列表。 */
  onGone: () => void
}

export function UserDetailDrawer({ user, returnFocusTo, onClose, onSync, onGone }: UserDetailDrawerProps) {
  // 每个 mutation 各持错误位：失败的文案贴着触发它的控件。
  // 组件按用户重挂载（容器给 key），这些状态不跨行残留。
  const [roleDraft, setRoleDraft] = useState<UserRole>(user?.role ?? 'user')
  const [roleError, setRoleError] = useState<string | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [statusOpen, setStatusOpen] = useState(false)
  const [resetError, setResetError] = useState<string | null>(null)
  const [resetOpen, setResetOpen] = useState(false)
  const [tempPassword, setTempPassword] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [restoreError, setRestoreError] = useState<string | null>(null)

  const roleMut = useMutation({
    mutationFn: (input: { user: User; role: UserRole }) =>
      updateUser(input.user.id, { role: input.role }),
    onSuccess: (updated) => {
      setRoleError(null)
      onSync(updated)
      notifySuccess(`已保存 ${updated.email} 的角色`)
    },
    onError: (error) => {
      setRoleError(isConflict(error) ? LAST_MASTER_TEXT : describeApiError(error))
    },
  })

  const statusMut = useMutation({
    mutationFn: (input: { user: User; disabled: boolean }) =>
      updateUser(input.user.id, { status: input.disabled ? 'disabled' : 'active' }),
    onSuccess: (updated, input) => {
      setStatusError(null)
      setStatusOpen(false)
      onSync(updated)
      notifySuccess(input.disabled ? `已停用 ${updated.email}` : `已启用 ${updated.email}`)
    },
    onError: (error) => {
      setStatusError(isConflict(error) ? LAST_MASTER_TEXT : describeApiError(error))
    },
  })

  const resetMut = useMutation({
    mutationFn: (target: User) => resetUserPassword(target.id),
    onSuccess: (result) => {
      setResetError(null)
      setResetOpen(false)
      // 单次展示：口令只进这一个状态位，关闭即弃。
      setTempPassword(result.temp_password)
    },
    onError: (error) => {
      setResetError(describeApiError(error))
    },
  })

  const deleteMut = useMutation({
    mutationFn: (target: User) => deleteUser(target.id),
    onSuccess: () => {
      setDeleteError(null)
      setDeleteOpen(false)
      onGone()
      notifySuccess(`已删除 ${user?.email ?? ''}`)
    },
    onError: (error) => {
      setDeleteError(isConflict(error) ? OWNED_ACCOUNTS_TEXT : describeApiError(error))
    },
  })

  const restoreMut = useMutation({
    mutationFn: (target: User) => restoreUser(target.id),
    onSuccess: (updated) => {
      setRestoreError(null)
      onSync(updated)
      notifySuccess(`已恢复 ${updated.email}`)
    },
    onError: (error) => {
      setRestoreError(isConflict(error) ? '该邮箱已被其他用户使用，无法恢复' : describeApiError(error))
    },
  })

  const closeOneTime = () => {
    // 关闭即弃：temp_password 不允许第二次看到。
    setTempPassword(null)
  }

  return (
    <>
      <Drawer
        open={user !== null}
        onOpenChange={(open) => {
          if (!open) onClose()
        }}
        title="管理用户"
        description={user?.email}
        returnFocusTo={returnFocusTo}
        footer={
          <Button variant="ghost" onClick={onClose}>
            关闭
          </Button>
        }
      >
        {user ? (
          tempPassword ? (
            <div className="app-detail-block">
              <Alert tone="warning" title="一次性临时口令">
                关闭后不可再查看。请立即复制并交给该用户，下次登录时必须改密。
              </Alert>
              <p className="data-cell-mono app-temp-password">{tempPassword}</p>
              <div className="app-detail-actions">
                <BusyButton
                  variant="primary"
                  busy={false}
                  onClick={() => {
                    void navigator.clipboard.writeText(tempPassword).then(
                      () => notifySuccess('已复制临时口令'),
                      () => notifySuccess('复制失败，请手动选择文本'),
                    )
                  }}
                >
                  复制
                </BusyButton>
                <Button variant="ghost" onClick={closeOneTime}>
                  我已保存，返回
                </Button>
              </div>
            </div>
          ) : (
            <div className="app-detail-block">
              <dl className="app-detail-list">
                <dt>邮箱</dt>
                <dd className="data-cell-mono">{user.email}</dd>
                <dt>角色</dt>
                <dd>{roleLabel(user.role)}</dd>
                <dt>状态</dt>
                <dd>
                  <Badge tone={user.status === 'active' ? 'success' : 'warning'} dot>
                    {user.status === 'active' ? '正常' : '停用'}
                  </Badge>
                  {user.deleted_at ? <Badge tone="danger">已删除</Badge> : null}
                </dd>
                <dt>创建时间</dt>
                <dd>{formatDateTime(user.created_at)}</dd>
                <dt>上次登录</dt>
                <dd>{formatDateTime(user.last_login_at)}</dd>
              </dl>

              {user.deleted_at ? (
                <div className="app-detail-actions">
                  <BusyButton
                    variant="primary"
                    busy={restoreMut.isPending}
                    busyLabel="恢复中…"
                    onClick={() => {
                      setRestoreError(null)
                      restoreMut.mutate(user)
                    }}
                  >
                    恢复用户
                  </BusyButton>
                </div>
              ) : (
                <>
                  <div className="app-detail-field">
                    <label htmlFor="user-drawer-role">角色变更</label>
                    <div className="app-detail-actions">
                      <select
                        id="user-drawer-role"
                        className="select"
                        value={roleDraft}
                        onChange={(event) => setRoleDraft(event.target.value as UserRole)}
                      >
                        {USER_ROLE_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                      <BusyButton
                        variant="primary"
                        busy={roleMut.isPending}
                        busyLabel="保存中…"
                        onClick={() => {
                          if (!user) return
                          setRoleError(null)
                          roleMut.mutate({ user, role: roleDraft })
                        }}
                      >
                        保存
                      </BusyButton>
                    </div>
                    {roleError ? (
                      <div role="alert">
                        <Alert tone="danger">{roleError}</Alert>
                      </div>
                    ) : null}
                  </div>

                  <div className="app-detail-actions">
                    <BusyButton
                      variant="outline-danger"
                      busy={resetMut.isPending}
                      busyLabel="生成中…"
                      onClick={() => {
                        setResetError(null)
                        setResetOpen(true)
                      }}
                    >
                      重置口令
                    </BusyButton>
                    <BusyButton
                      variant="outline-danger"
                      busy={false}
                      onClick={() => {
                        setStatusError(null)
                        setStatusOpen(true)
                      }}
                    >
                      {user.status === 'active' ? '停用用户' : '启用用户'}
                    </BusyButton>
                    <BusyButton
                      variant="outline-danger"
                      busy={deleteMut.isPending}
                      busyLabel="删除中…"
                      onClick={() => {
                        setDeleteError(null)
                        setDeleteOpen(true)
                      }}
                    >
                      删除用户
                    </BusyButton>
                  </div>
                  {resetError ? (
                    <div role="alert">
                      <Alert tone="danger">{resetError}</Alert>
                    </div>
                  ) : null}
                  {restoreError ? (
                    <div role="alert">
                      <Alert tone="danger">{restoreError}</Alert>
                    </div>
                  ) : null}
                </>
              )}
            </div>
          )
        ) : null}
      </Drawer>

      <ConfirmModal
        open={user !== null && statusOpen}
        title={user?.status === 'active' ? '停用用户' : '启用用户'}
        description={
          user?.status === 'active'
            ? `停用后 ${user?.email ?? ''} 的全部会话立即失效，登录被拒绝。`
            : `启用后 ${user?.email ?? ''} 可以重新登录。`
        }
        confirmText={user?.status === 'active' ? '停用' : '启用'}
        tone="danger"
        pending={statusMut.isPending}
        error={statusError}
        layer={2}
        returnFocusTo={returnFocusTo}
        onClose={() => {
          setStatusError(null)
          setStatusOpen(false)
        }}
        onConfirm={() => {
          if (!user) return
          statusMut.mutate({ user, disabled: user.status === 'active' })
        }}
      />

      <ConfirmModal
        open={user !== null && resetOpen}
        title="重置口令"
        description={`成功后该用户全部会话失效，并收到一个一次性临时口令。临时口令只显示一次，关闭后不可再查看。`}
        confirmText="生成临时口令"
        tone="danger"
        pending={resetMut.isPending}
        error={resetError}
        layer={2}
        returnFocusTo={returnFocusTo}
        onClose={() => {
          setResetError(null)
          setResetOpen(false)
        }}
        onConfirm={() => {
          if (!user) return
          resetMut.mutate(user)
        }}
      />

      <ConfirmModal
        open={user !== null && deleteOpen}
        title="删除用户"
        description={`软删后 ${user?.email ?? ''} 的全部会话失效，列表默认不再显示；可在「含已删除」中恢复。名下账号不受影响。`}
        confirmText="删除"
        tone="danger"
        pending={deleteMut.isPending}
        error={deleteError}
        layer={2}
        returnFocusTo={returnFocusTo}
        onClose={() => {
          setDeleteError(null)
          setDeleteOpen(false)
        }}
        onConfirm={() => {
          if (!user) return
          deleteMut.mutate(user)
        }}
      />
    </>
  )
}
