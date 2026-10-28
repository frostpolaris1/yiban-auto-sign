/**
 * **功能**
 * 学校管理员模态：全量列表、添加绑定、请求移除。
 *
 * **归属**
 * `features/schools` 展示层：取数与写操作由调用方持有。
 *
 * **口径**
 * 打开即按 `next_cursor` 拉全部页，封顶 10 页（`lib/api/school`）。
 * 添加只收 user_id：契约没有用户选择器，也没有邀请流。
 * 移除先发到调用方的确认模态；本组件不发 DELETE。
 * 展示构建只读：添加与移除都置灰，原因写在添加按钮下方。
 */

import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { School, SchoolAdmin } from '@/lib/api/school'
import { listAllSchoolAdmins } from '@/lib/api/school'
import { describeApiError } from '@/lib/api/client'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import { formatDateTime } from '@/lib/format'
import { roleLabel } from '@/lib/identity'
import { READ_ONLY_REASON, SHOWCASE } from '@/lib/showcase-mode'
import { showcaseSchoolAdmins } from '@/lib/showcase'

type SchoolAdminsModalProps = {
  school: School | null
  adding: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onAdd: (userId: string) => void
  onRemove: (admin: SchoolAdmin, opener: HTMLElement) => void
}

export function SchoolAdminsModal({
  school,
  adding,
  error,
  returnFocusTo,
  onClose,
  onAdd,
  onRemove,
}: SchoolAdminsModalProps) {
  // 调用方按学校 ID 换 key：换学校即清空输入。
  const [userId, setUserId] = useState('')
  // 已提交的值：成功后才清空输入，失败时保留供修改后重试。
  const [submitted, setSubmitted] = useState<string | null>(null)

  useEffect(() => {
    if (submitted === null || adding) return
    if (error === null) setUserId('')
    setSubmitted(null)
  }, [submitted, adding, error])

  const adminsQuery = useQuery({
    queryKey: ['school-admins', school?.id ?? null],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseSchoolAdmins()) : listAllSchoolAdmins(school!.id),
    enabled: school !== null,
    staleTime: 0,
  })

  const admins = adminsQuery.data ?? []
  const listError = adminsQuery.isError ? describeApiError(adminsQuery.error) : null
  const readOnlyReason = SHOWCASE ? READ_ONLY_REASON : null

  return (
    <Modal
      open={school !== null}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title="学校管理员"
      description={school ? `学校 ${school.name} · ${school.code}` : undefined}
      returnFocusTo={returnFocusTo}
      footer={
        <Button variant="ghost" onClick={onClose} disabled={adding}>
          关闭
        </Button>
      }
    >
      <div className="app-admin-add">
        <TextInput
          id="school-admin-user-id"
          label="用户 ID"
          required
          placeholder="user-id"
          hint="契约无用户选择器。用户须已存在，角色为 admin 或 master。"
          value={userId}
          onChange={(event) => setUserId(event.target.value)}
        />
        <Button
          variant="primary"
          disabled={adding || userId.trim() === '' || readOnlyReason !== null}
          title={readOnlyReason ?? undefined}
          onClick={() => {
            const id = userId.trim()
            if (id === '') return
            setSubmitted(id)
            onAdd(id)
          }}
        >
          添加管理员
        </Button>
        {readOnlyReason ? <span className="app-deny-note">{readOnlyReason}</span> : null}
      </div>

      {error ? (
        <div className="app-modal-error">
          <Alert tone="danger" title="请求失败">
            {error}
          </Alert>
        </div>
      ) : null}

      {listError ? (
        <div className="app-modal-error">
          <Alert tone="danger" title="管理员列表加载失败">
            {listError}
          </Alert>
        </div>
      ) : null}

      {!listError && adminsQuery.isLoading ? <p className="app-muted">加载中…</p> : null}

      {!listError && !adminsQuery.isLoading && admins.length === 0 ? (
        <p className="app-muted">暂无绑定管理员。</p>
      ) : null}

      {admins.length > 0 ? (
        <ul className="app-admin-list">
          {admins.map((admin) => (
            <li key={admin.user_id} className="app-admin-row">
              <div className="app-admin-main">
                <span className="data-cell-mono">{admin.email}</span>
                <span className="app-caption">
                  {roleLabel(admin.role)} · 由 {admin.granted_by} 授予 ·{' '}
                  {formatDateTime(admin.created_at)}
                </span>
              </div>
              <Button
                variant="ghost"
                size="sm"
                aria-label={`移除 ${admin.email}`}
                title={readOnlyReason ?? undefined}
                disabled={adding || readOnlyReason !== null}
                onClick={(event) => {
                  event.stopPropagation()
                  onRemove(admin, event.currentTarget)
                }}
              >
                移除
              </Button>
            </li>
          ))}
        </ul>
      ) : null}
    </Modal>
  )
}
