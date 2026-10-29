/**
 * **功能**
 * 用户管理页装配：角色准入、筛选、游标翻页与只读列表。
 *
 * **归属**
 * `features/user` 域容器：页面只引用它。
 *
 * **口径**
 * 角色来自 `GET /api/auth/me`。无 view 权限时不发用户列表请求，
 * 只渲染无权限卡片；导航项由 `nav.ts` 的角色元数据隐藏。
 * 本页只读：没有写操作，也没有建号入口。
 * 搜索输入防抖 250ms；回车立即生效，不等防抖。
 * 任一筛选变化回到第一页：游标栈清空。
 * 游标只活在组件状态里，不写进 URL。
 * 展示构建（`VITE_SHOWCASE=1`）取合成夹具，零网络请求。
 */

import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card } from '@/components/ui/Card'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { PageHeader } from '@/components/ui/PageHeader'
import type { Role } from '@/lib/api/auth'
import { describeApiError } from '@/lib/api/client'
import { listUsers, type UserRole, type UserStatus } from '@/lib/api/user'
import { currentCursor, emptyCursorStack, goNext, goPrevious, hasPreviousPage } from '@/lib/cursor'
import { USER_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { SHOWCASE, useMe } from '@/lib/session'
import { showcaseUsers } from '@/lib/showcase'
import { UserFilters, type UserFilterValues } from './UserFilters'
import { UsersTable } from './UsersTable'

/** 输入防抖窗口。上限由派单钉在 300ms。 */
const SEARCH_DEBOUNCE_MS = 250

const DENY_REASON = '你的角色无权查看用户管理'

export function UsersView() {
  const meQuery = useMe()
  const role: Role | null = meQuery.data?.role ?? null
  const allowed = roleAllowed(role, USER_VIEW_ROLES)

  const [filters, setFilters] = useState<UserFilterValues>({})
  const [qInput, setQInput] = useState('')
  const [stack, setStack] = useState(emptyCursorStack)
  const cursor = currentCursor(stack)

  /** 提交当前输入框的值。值与已生效值相同也回第一页，末尾位置不保留。 */
  const commitQuery = () => {
    const next = qInput.trim() || undefined
    setFilters((previous) => (previous.q === next ? previous : { ...previous, q: next }))
    setStack(emptyCursorStack())
  }

  useEffect(() => {
    const next = qInput.trim() || undefined
    if (next === filters.q) return
    const timer = window.setTimeout(() => {
      setFilters((previous) => ({ ...previous, q: next }))
      setStack(emptyCursorStack())
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [qInput, filters.q])

  const usersQuery = useQuery({
    queryKey: [
      'users-page',
      filters.role ?? null,
      filters.status ?? null,
      filters.q ?? null,
      cursor ?? null,
    ],
    // 列表随时可变。缓存键回到旧页时必须重新取数，不能给旧数据。
    staleTime: 0,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcaseUsers())
      return listUsers({
        role: filters.role,
        status: filters.status,
        q: filters.q,
        cursor,
      })
    },
    enabled: allowed,
  })

  const rows = usersQuery.data?.items ?? []
  const error = usersQuery.isError ? describeApiError(usersQuery.error) : null

  const changeRole = (next?: UserRole) => {
    setFilters((previous) => ({ ...previous, role: next }))
    setStack(emptyCursorStack())
  }

  const changeStatus = (next?: UserStatus) => {
    setFilters((previous) => ({ ...previous, status: next }))
    setStack(emptyCursorStack())
  }

  return (
    <>
      <PageHeader
        eyebrow="系统 · 用户"
        title={
          <>
            用户 <span className="accent">管理</span>
          </>
        }
        subtitle="master 可查看全部账号的邮箱、角色与状态。本页只读。"
      />

      {!allowed ? (
        <DeniedCard eyebrow="Users" reason={DENY_REASON} />
      ) : (
        <Card eyebrow="Users" title="用户列表" action={<span className="app-caption">游标分页</span>}>
          <UserFilters
            values={filters}
            qInput={qInput}
            onRoleChange={changeRole}
            onStatusChange={changeStatus}
            onQInputChange={setQInput}
            onQCommit={commitQuery}
          />
          <UsersTable
            rows={rows}
            loading={usersQuery.isLoading || usersQuery.isFetching}
            error={error}
            pageIndex={stack.index}
            hasPrevious={hasPreviousPage(stack)}
            hasNext={Boolean(usersQuery.data?.next_cursor) && !usersQuery.isFetching}
            onPrevious={() => setStack(goPrevious)}
            onNext={() => {
              const next = usersQuery.data?.next_cursor
              if (!next) return
              setStack((previous) => goNext(previous, next))
            }}
            onRetry={() => {
              void usersQuery.refetch()
            }}
          />
        </Card>
      )}
    </>
  )
}
