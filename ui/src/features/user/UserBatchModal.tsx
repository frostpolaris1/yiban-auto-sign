/**
 * **功能**
 * 用户批量动作的确认弹窗：影响范围、二次口令、逐项结果。
 *
 * **归属**
 * `features/user` 展示层：请求由 `UsersView` 发。
 *
 * **口径**
 * 确认前零请求；口令只存在于本弹窗，关闭与成功都清空。
 * reset_password 成功项各带一枚一次性临时口令：只在结果清单出现一次，
 * 逐条可复制；全成功时弹窗也不自动关，操作者点「完成」才关闭并从父状态清掉。
 * 部分失败时同样保留结果清单：按请求顺序给 ok/error。
 */

import { useEffect, useState } from 'react'
import type { User, UserBatchAction, UserBatchResult } from '@/lib/api/user'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { PasswordInput } from '@/components/ui/Input'
import { notifySuccess } from '@/components/ui/toast'

export const USER_BATCH_LABELS: Record<UserBatchAction, string> = {
  disable: '停用',
  enable: '启用',
  reset_password: '重置口令',
  delete: '删除',
  restore: '恢复',
}

const DESCRIPTIONS: Record<UserBatchAction, string> = {
  disable: '停用后每个用户全部会话立即失效，登录被拒绝。',
  enable: '启用后用户可以重新登录。',
  reset_password: '每个成功项生成一枚一次性临时口令，只显示这一次。',
  delete: '软删后全部会话失效；名下账号不受影响。',
  restore: '恢复后角色与状态保持删除前值；邮箱被占用或容量满的项会失败。',
}

type UserBatchModalProps = {
  open: boolean
  action: UserBatchAction | null
  rows: User[]
  pending: boolean
  error: string | null
  result: UserBatchResult | null
  onClose: () => void
  onConfirm: (password: string) => void
}

export function UserBatchModal({
  open,
  action,
  rows,
  pending,
  error,
  result,
  onClose,
  onConfirm,
}: UserBatchModalProps) {
  const [password, setPassword] = useState('')

  useEffect(() => {
    if (!open) setPassword('')
  }, [open])
  useEffect(() => {
    setPassword('')
  }, [action])
  // 成功态保留结果页期间操作者口令已无用：结果一到即清。
  useEffect(() => {
    if (result) setPassword('')
  }, [result])

  const labels = new Map(rows.map((row) => [row.id, row.email]))
  const label = action ? USER_BATCH_LABELS[action] : '批量操作'

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        // 在途与结果态都锁关：X/Esc/遮罩不得丢弃进行中或一次性口令结果；只有「完成」关闭。
        if (!next && !pending && !result) onClose()
      }}
      title={`批量${label}？`}
      description={
        action
          ? `将作用于已选 ${rows.length} 个用户。${DESCRIPTIONS[action]}`
          : undefined
      }
      footer={
        result ? (
          <Button variant="ghost" onClick={onClose}>
            完成
          </Button>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose} disabled={pending}>
              取消
            </Button>
            <Button
              variant={action === 'delete' || action === 'reset_password' ? 'danger' : 'primary'}
              disabled={pending || password.trim() === ''}
              onClick={() => onConfirm(password)}
            >
              {pending ? '执行中…' : `确认${label}`}
            </Button>
          </>
        )
      }
    >
      <ul className="app-batch-impact" aria-label="影响范围">
        {rows.map((row) => (
          <li key={row.id} className="data-cell-mono">
            {row.email}
          </li>
        ))}
      </ul>

      {result ? (
        <div className="app-batch-result" role="status" aria-label="批量结果">
          <strong>
            结果：{result.items.filter((item) => item.status === 'ok').length} 条成功，
            {result.items.filter((item) => item.status === 'error').length} 条失败
          </strong>
          {result.items.map((item) => {
            const email = labels.get(item.id) ?? item.id
            const tempPassword = item.temp_password
            return (
              <div key={item.id} className="app-batch-result-row">
                <span className="data-cell-mono">{email}</span>
                {item.status === 'ok' ? (
                  tempPassword ? (
                    <span className="app-batch-result-secret">
                      成功 · 临时口令{' '}
                      <span className="data-cell-mono">{tempPassword}</span>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`复制 ${email} 的临时口令`}
                        onClick={() => {
                          void navigator.clipboard.writeText(tempPassword).then(
                            () => notifySuccess('已复制临时口令'),
                            () => notifySuccess('复制失败，请手动选择文本'),
                          )
                        }}
                      >
                        复制
                      </Button>
                      <span className="app-muted">（完成前请逐条保存；关闭后不可再查看）</span>
                    </span>
                  ) : (
                    <span>成功</span>
                  )
                ) : (
                  <span>
                    {item.code ?? 'error'} · {item.message ?? '未提供原因'}
                  </span>
                )}
              </div>
            )
          })}
        </div>
      ) : (
        <div className="app-form-stack">
          <PasswordInput
            id="user-batch-password"
            label="你的登录口令"
            hint="批量动作需要二次确认；口令不会保存。"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
      )}

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
