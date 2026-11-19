/**
 * **功能**
 * 学校管理员模态：全量列表、候选/检索选择器、添加绑定、请求移除。
 *
 * **归属**
 * `features/schools` 展示层：取数与写操作由调用方持有。
 *
 * **口径**
 * 打开即按 `next_cursor` 拉全部页，封顶 10 页（`lib/api/school`）。
 * 管理员选择器（C7）：空输入给候选（现有 admin/master 用户，
 * 按最近登录排序，取前 6 个，已绑定者剔除）；输入走 `/api/users/lookup`；
 * 无命中给「没有匹配用户」；选中后可清除；移除必经确认弹窗。
 * 绑定是危险写：必须填操作者本人本站登录口令；口令只存在于本模态，
 * 成功或关闭即清空。
 * 展示构建只读：添加与移除都置灰，原因写在添加按钮下方。
 */

import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { School, SchoolAdmin } from '@/lib/api/school'
import { listAllSchoolAdmins } from '@/lib/api/school'
import { describeApiError } from '@/lib/api/client'
import { listUsers, lookupUsers, type UserStatus } from '@/lib/api/user'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { TextInput, PasswordInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import { formatDateTime } from '@/lib/format'
import { roleLabel, userStatusLabel } from '@/lib/identity'
import { READ_ONLY_REASON, SHOWCASE } from '@/lib/showcase-mode'
import { showcaseSchoolAdmins } from '@/lib/showcase'

const LOOKUP_DEBOUNCE_MS = 300
const CANDIDATE_LIMIT = 6

type AdminCandidate = {
  id: string
  email_masked: string
  status: UserStatus
}

/** 完整邮箱只用于本机展示前掩码；服务端 lookup 也只回掩码。 */
function maskEmail(email: string): string {
  const at = email.indexOf('@')
  if (at <= 0) return email
  return `${email.slice(0, 1)}***${email.slice(at)}`
}

type SchoolAdminsModalProps = {
  school: School | null
  adding: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  /** label 是掩码邮箱，只用于成功 toast，不进请求体。 */
  onAdd: (userId: string, password: string, label: string) => void
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
  // 调用方按学校 ID 换 key：换学校即清空选择与输入。
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [selected, setSelected] = useState<AdminCandidate | null>(null)
  const [password, setPassword] = useState('')

  const adminsQuery = useQuery({
    queryKey: ['school-admins', school?.id ?? null],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseSchoolAdmins()) : listAllSchoolAdmins(school!.id),
    enabled: school !== null,
    staleTime: 0,
  })

  // 空输入候选：现有 admin/master 用户按最近登录排序；展示构建零请求。
  const adminUsers = useQuery({
    queryKey: ['school-admin-candidates', 'admin'],
    queryFn: () => listUsers({ role: 'admin', limit: 50 }),
    enabled: school !== null && !SHOWCASE,
    staleTime: 60_000,
  })
  const masterUsers = useQuery({
    queryKey: ['school-admin-candidates', 'master'],
    queryFn: () => listUsers({ role: 'master', limit: 50 }),
    enabled: school !== null && !SHOWCASE,
    staleTime: 60_000,
  })

  // 300ms 防抖：只在非空输入上检索。
  useEffect(() => {
    const trimmed = query.trim()
    if (trimmed === debounced) return
    const timer = window.setTimeout(() => setDebounced(trimmed), LOOKUP_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [query, debounced])

  const lookup = useQuery({
    queryKey: ['school-admin-lookup', debounced],
    queryFn: () => lookupUsers(debounced, { limit: 8 }),
    enabled: school !== null && debounced.length > 0 && !SHOWCASE,
  })

  const admins = adminsQuery.data ?? []
  const boundIds = new Set(admins.map((admin) => admin.user_id))
  const listError = adminsQuery.isError ? describeApiError(adminsQuery.error) : null
  const readOnlyReason = SHOWCASE ? READ_ONLY_REASON : null

  const candidates: AdminCandidate[] = [
    ...(adminUsers.data?.items ?? []),
    ...(masterUsers.data?.items ?? []),
  ]
    .filter((user) => user.deleted_at === null && !boundIds.has(user.id))
    .sort((left, right) => (right.last_login_at ?? '').localeCompare(left.last_login_at ?? ''))
    .slice(0, CANDIDATE_LIMIT)
    .map((user) => ({
      id: user.id,
      email_masked: maskEmail(user.email),
      status: user.status,
    }))

  const hits: AdminCandidate[] = (lookup.data?.items ?? [])
    .filter((item) => !boundIds.has(item.id))
    .map((item) => ({
      id: item.id,
      email_masked: item.email_masked,
      status: item.status,
    }))

  const trimmedQuery = query.trim()
  const searching = trimmedQuery !== ''

  // 关闭即清空：口令、已选与检索词不跨次驻留（父层按同一实例复用本组件）。
  useEffect(() => {
    if (school === null) {
      setPassword('')
      setSelected(null)
      setQuery('')
      setDebounced('')
    }
  }, [school])

  // 成功信号以服务端列表为准：刷新后的绑定列表出现所选用户，
  // 才清空选择与口令。失败时选择保留，操作者改口令即可重试。
  useEffect(() => {
    if (!selected || !adminsQuery.data) return
    if (adminsQuery.data.some((admin) => admin.user_id === selected.id)) {
      setSelected(null)
      setPassword('')
      setQuery('')
      setDebounced('')
    }
  }, [adminsQuery.data, selected])

  const results = searching ? hits : candidates
  const pick = (candidate: AdminCandidate) => {
    setSelected(candidate)
    setQuery('')
    setDebounced('')
  }

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
      <div className="app-admin-picker">
        {selected ? (
          <div className="app-admin-picker-selected">
            <span>已选</span>
            <span className="data-cell-mono">{selected.email_masked}</span>
            <Badge tone={selected.status === 'active' ? 'success' : 'warning'} dot>
              {userStatusLabel(selected.status)}
            </Badge>
            <Button
              variant="ghost"
              size="sm"
              disabled={adding}
              onClick={() => {
                setSelected(null)
                setPassword('')
              }}
            >
              清除
            </Button>
          </div>
        ) : (
          <>
            <TextInput
              id="school-admin-search"
              label="搜索用户邮箱"
              placeholder="输入邮箱前缀"
              hint="空输入给出候选（现有管理员，按最近登录）；输入后按前缀检索。"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            {searching && lookup.isError ? (
              <span className="app-deny-note">用户检索失败，请稍后重试</span>
            ) : searching && lookup.isFetching ? (
              <span className="app-muted">查询中…</span>
            ) : results.length === 0 && searching ? (
              <span className="app-deny-note">没有匹配用户</span>
            ) : results.length === 0 && !searching && (adminUsers.isLoading || masterUsers.isLoading) ? (
              <span className="app-muted">候选加载中…</span>
            ) : results.length === 0 ? (
              <span className="app-muted">暂无候选管理员，输入邮箱检索。</span>
            ) : (
              <ul className="app-admin-picker-results" aria-label={searching ? '检索结果' : '候选管理员'}>
                {results.map((candidate) => (
                  <li key={candidate.id}>
                    <span>
                      <span className="data-cell-mono">{candidate.email_masked}</span>{' '}
                      <Badge tone={candidate.status === 'active' ? 'success' : 'warning'} dot>
                        {userStatusLabel(candidate.status)}
                      </Badge>
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`选择 ${candidate.email_masked}`}
                      disabled={adding}
                      onClick={() => pick(candidate)}
                    >
                      选择
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        <PasswordInput
          id="school-admin-password"
          label="你的登录口令"
          hint="绑定学校管理员需要二次确认；口令不会保存。"
          required
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />

        <Button
          variant="primary"
          disabled={
            adding ||
            selected === null ||
            password.trim() === '' ||
            readOnlyReason !== null
          }
          title={readOnlyReason ?? undefined}
          onClick={() => {
            if (!selected) return
            onAdd(selected.id, password, selected.email_masked)
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
