/**
 * **功能**
 * 用户管理页装配：角色准入、URL 筛选、游标翻页、建号与右侧抽屉。
 *
 * **归属**
 * `features/user` 域容器：页面只引用它。
 *
 * **口径**
 * 角色来自 `GET /api/auth/me`。无 view 权限时不发用户列表请求，
 * 只渲染无权限卡片；导航项由 `nav.ts` 的角色元数据隐藏。
 * URL 是筛选与游标的唯一事实源：`?role&status&q&deleted&cursor`。
 * 筛选与游标的写回一律 push：后退＝撤销最近一次筛选或翻页。
 * 解析与序列化在 `urlState.ts`；路由的写回由 `UsersPage` 注入。
 * 搜索输入防抖 250ms；回车立即生效，不等防抖。
 * 任一筛选变化回到第一页：游标从 URL 移除。
 * 游标栈只在会话内记忆「上一页」；刷新后从 URL 游标重建，只能前进。
 * 写操作成功的反馈走 toast；失败留在原控件旁，可回看。
 * 展示构建（`VITE_SHOWCASE=1`）取合成夹具，零网络请求。
 */

import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { BusyButton } from '@/components/ui/BusyButton'
import { Card } from '@/components/ui/Card'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { PageHeader } from '@/components/ui/PageHeader'
import { notifySuccess } from '@/components/ui/toast'
import type { Role } from '@/lib/api/auth'
import { describeApiError } from '@/lib/api/client'
import { listUsers, type User } from '@/lib/api/user'
import { currentCursor, emptyCursorStack, goNext, goPrevious, hasPreviousPage, type CursorStack } from '@/lib/cursor'
import { USER_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { SHOWCASE, useMe } from '@/lib/session'
import { showcaseUsers } from '@/lib/showcase'
import { UserDetailDrawer } from './UserDetailDrawer'
import { UserCreateModal } from './UserCreateModal'
import { UserFilters } from './UserFilters'
import { UsersTable } from './UsersTable'
import { withFilter, type UserURLState } from './urlState'
import { AsyncState } from '@/components/ui/AsyncState'

/** 输入防抖窗口。上限由派单钉在 300ms。 */
const SEARCH_DEBOUNCE_MS = 250

const DENY_REASON = '你的角色无权查看用户管理'

export type UsersViewProps = {
  urlState: UserURLState
  onSearchChange: (next: UserURLState, opts?: { replace?: boolean }) => void
}

export function UsersView({ urlState, onSearchChange }: UsersViewProps) {
  const queryClient = useQueryClient()
  const meQuery = useMe()
  const role: Role | null = meQuery.data?.role ?? null
  const allowed = roleAllowed(role, USER_VIEW_ROLES)

  const [qInput, setQInput] = useState(urlState.q ?? '')
  // URL 是输入框的回灌源（独立审查 P2-5）：后退清掉 ?q 时输入框必须同步清空，
  // 否则 250ms 后防抖会把旧值写回 URL，后退等于失效。
  const urlQuery = urlState.q
  useEffect(() => {
    setQInput(urlQuery ?? '')
  }, [urlQuery])
  // 游标栈只在会话内记「上一页」；URL 是当前页的游标事实源。
  const [stack, setStack] = useState<CursorStack>(() =>
    urlState.cursor ? { cursors: [undefined, urlState.cursor], index: 1 } : emptyCursorStack(),
  )
  const urlCursor = urlState.cursor
  useEffect(() => {
    setStack((previous) => {
      if (currentCursor(previous) === (urlCursor ?? undefined)) return previous
      return urlCursor ? { cursors: [undefined, urlCursor], index: 1 } : emptyCursorStack()
    })
  }, [urlCursor])

  const cursor = currentCursor(stack)

  /** 回车是显式动作：同值也回第一页，末尾位置不保留（独立审查 P3）。 */
  const commitQuery = () => {
    const next = qInput.trim() || undefined
    onSearchChange(withFilter(urlState, { q: next }))
  }

  useEffect(() => {
    const next = qInput.trim() || undefined
    if (next === (urlState.q ?? undefined)) return
    const timer = window.setTimeout(() => {
      onSearchChange(withFilter(urlState, { q: next }))
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [qInput, urlState, onSearchChange])

  const usersQuery = useQuery({
    queryKey: [
      'users-page',
      urlState.role ?? null,
      urlState.status ?? null,
      urlState.q ?? null,
      urlState.deleted ? 'deleted' : null,
      cursor ?? null,
    ],
    // 列表随时可变。缓存键回到旧页时必须重新取数，不能给旧数据。
    staleTime: 0,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcaseUsers())
      return listUsers({
        role: urlState.role,
        status: urlState.status,
        q: urlState.q,
        include_deleted: urlState.deleted,
        cursor,
      })
    },
    enabled: allowed,
  })

  const invalidateUsers = () => {
    void queryClient.invalidateQueries({ queryKey: ['users-page'] })
  }

  const [detailUser, setDetailUser] = useState<User | null>(null)
  const [drawerReturnFocus, setDrawerReturnFocus] = useState<HTMLElement | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createReturnFocus, setCreateReturnFocus] = useState<HTMLElement | null>(null)

  const openDetail = (user: User, opener: HTMLElement | null) => {
    setDetailUser(user)
    setDrawerReturnFocus(opener)
  }

  const rows = usersQuery.data?.items ?? []
  const error = usersQuery.isError ? describeApiError(usersQuery.error) : null

  const listStatus: 'loading' | 'error' | 'empty' | 'data' = usersQuery.isPending
    ? 'loading'
    : usersQuery.isError
      ? 'error'
      : rows.length === 0
        ? 'empty'
        : 'data'

  return (
    <>
      <PageHeader
        eyebrow="系统 · 用户"
        title={
          <>
            用户 <span className="accent">管理</span>
          </>
        }
        subtitle="master 可查看全部账号的邮箱、角色与状态。"
        actions={
          allowed ? (
            <BusyButton
              variant="primary"
              onClick={(event) => {
                setCreateReturnFocus(event.currentTarget)
                setCreateOpen(true)
              }}
            >
              新建用户
            </BusyButton>
          ) : undefined
        }
      />

      {!allowed ? (
        <DeniedCard eyebrow="Users" reason={DENY_REASON} />
      ) : (
        <Card eyebrow="Users" title="用户列表" action={<span className="app-caption">游标分页</span>}>
          <UserFilters
            values={urlState}
            qInput={qInput}
            // 筛选写 URL 一律 push：后退＝撤销最近一次筛选。
            onRoleChange={(next) => onSearchChange(withFilter(urlState, { role: next }))}
            onStatusChange={(next) => onSearchChange(withFilter(urlState, { status: next }))}
            onDeletedChange={(next) => onSearchChange(withFilter(urlState, { deleted: next }))}
            onQInputChange={setQInput}
            onQCommit={commitQuery}
          />
          <AsyncState
            status={listStatus}
            error={error ?? undefined}
            emptyLabel={
              <>
                <div>没有匹配的用户</div>
                <div className="app-muted">调整筛选条件后重试。</div>
              </>
            }
            onRetry={
              usersQuery.isError
                ? () => {
                    void usersQuery.refetch()
                  }
                : undefined
            }
          >
            <UsersTable
              rows={rows}
              pageIndex={stack.index}
              hasPrevious={hasPreviousPage(stack)}
              hasNext={Boolean(usersQuery.data?.next_cursor) && !usersQuery.isFetching}
              onPrevious={() => {
                const previous = goPrevious(stack)
                setStack(previous)
                const prev = currentCursor(previous)
                onSearchChange(
                  prev
                    ? { ...urlState, cursor: prev }
                    : { role: urlState.role, status: urlState.status, q: urlState.q, deleted: urlState.deleted },
                )
              }}
              onNext={() => {
                const next = usersQuery.data?.next_cursor
                if (!next) return
                setStack((previous) => goNext(previous, next))
                onSearchChange({ ...urlState, cursor: next })
              }}
              onManage={openDetail}
            />
          </AsyncState>
        </Card>
      )}

      <UserDetailDrawer
        key={detailUser?.id ?? 'none'}
        user={detailUser}
        returnFocusTo={drawerReturnFocus}
        onClose={() => setDetailUser(null)}
        onSync={(updated) => {
          setDetailUser(updated)
          invalidateUsers()
        }}
        onGone={() => {
          setDetailUser(null)
          invalidateUsers()
        }}
      />

      <UserCreateModal
        open={createOpen}
        returnFocusTo={createReturnFocus}
        onClose={() => setCreateOpen(false)}
        onCreated={(user) => {
          setCreateOpen(false)
          invalidateUsers()
          notifySuccess(`已创建 ${user.email}`)
        }}
      />
    </>
  )
}
