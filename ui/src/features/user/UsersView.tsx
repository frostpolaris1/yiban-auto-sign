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
 * 批量重置口令的临时口令只显示一次：全成功也保留结果清单，显式「完成」才关。
 * 列表每 10 秒条件轮询一次；模态或抽屉打开时暂停，展示模式不发请求。
 * 展示构建（`VITE_SHOWCASE=1`）取合成夹具，零网络请求。
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BusyButton } from '@/components/ui/BusyButton'
import { Card } from '@/components/ui/Card'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { PageHeader } from '@/components/ui/PageHeader'
import { SelectionBar } from '@/components/ui/SelectionBar'
import { notifySuccess } from '@/components/ui/toast'
import type { Role } from '@/lib/api/auth'
import { describeApiError, describeReauthError } from '@/lib/api/client'
import { batchUsers, listUsers, type User, type UserBatchAction, type UserBatchResult } from '@/lib/api/user'
import { currentCursor, emptyCursorStack, goNext, goPrevious, hasPreviousPage, type CursorStack } from '@/lib/cursor'
import { LIST_POLL_INTERVAL_MS } from '@/lib/query'
import { USER_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { SHOWCASE, useMe } from '@/lib/session'
import { READ_ONLY_REASON } from '@/lib/showcase-mode'
import { showcaseUsers } from '@/lib/showcase'
import { UserBatchModal } from './UserBatchModal'
import { UserDetailDrawer } from './UserDetailDrawer'
import { UserCreateModal } from './UserCreateModal'
import { UserFilters } from './UserFilters'
import { UserGroupTabs } from './UserGroupTabs'
import { UsersTable } from './UsersTable'
import { nextGroupWithMembers, USER_GROUP_LABELS } from './display'
import { withDeleted, withFilter, withGroup, type UserURLState } from './urlState'
import { AsyncState } from '@/components/ui/AsyncState'
import { Button } from '@/components/ui/Button'

/** 输入防抖窗口；上限 300ms。 */
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
  // URL 是输入框的回灌源：后退清掉 ?q 时输入框必须同步清空，
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

  // URL 的任何变化（筛选、分组、已删除、游标）都换一批行：选择不跨页、不跨视图。
  useEffect(() => {
    setSelectedIds(new Set<string>())
  }, [urlState.role, urlState.status, urlState.q, urlState.group, urlState.deleted, urlState.cursor])

  // 查询游标直接跟随 URL，不从游标栈取：栈由事件与 effect 更新，晚一拍
  // 会让改筛选的那次请求仍带旧游标，先拿错页再自我纠正（多发一条错页请求）。
  // 栈只负责「上一页」记忆与页码展示。
  const cursor = urlCursor

  /** 回车是显式动作：同值也回第一页，末尾位置不保留。 */
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

  const invalidateUsers = () => {
    void queryClient.invalidateQueries({ queryKey: ['users-page'] })
  }

  const [detailUser, setDetailUser] = useState<User | null>(null)
  const [drawerReturnFocus, setDrawerReturnFocus] = useState<HTMLElement | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createReturnFocus, setCreateReturnFocus] = useState<HTMLElement | null>(null)

  // 批量选择：只对 master 开放；切分组/筛选/翻页即清空。
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(() => new Set<string>())
  const [batch, setBatch] = useState<{ action: UserBatchAction; rows: User[] } | null>(null)
  const [batchError, setBatchError] = useState<string | null>(null)
  const [batchResult, setBatchResult] = useState<UserBatchResult | null>(null)

  const toggleRow = (id: string) => {
    setSelectedIds((previous) => {
      const next = new Set(previous)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAll = (ids: string[], checked: boolean) => {
    setSelectedIds(checked ? new Set(ids) : new Set<string>())
  }

  // 模态或抽屉打开时暂停轮询：后台刷新不得改写用户正在操作的行。
  const modalOpen = detailUser !== null || createOpen || batch !== null

  const usersQuery = useQuery({
    queryKey: [
      'users-page',
      urlState.role ?? null,
      urlState.status ?? null,
      urlState.q ?? null,
      urlState.group ?? null,
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
        group: urlState.group,
        include_deleted: urlState.deleted,
        cursor,
      })
    },
    enabled: allowed,
    // 每 10 秒轮询一次；展示模式与模态/抽屉打开时零请求。
    refetchInterval: SHOWCASE || modalOpen || !allowed ? false : LIST_POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
  })

  const openBatch = (action: UserBatchAction, rows: User[]) => {
    batchMutation.reset()
    setBatchError(null)
    setBatchResult(null)
    setBatch({ action, rows })
  }

  const closeBatch = () => {
    batchMutation.reset()
    setBatch(null)
    setBatchError(null)
    setBatchResult(null)
  }

  const batchMutation = useMutation({
    mutationFn: (input: { action: UserBatchAction; ids: string[]; password: string }) =>
      batchUsers({
        action: input.action,
        ids: input.ids,
        current_password: input.password,
      }),
    onSuccess: (result, input) => {
      invalidateUsers()
      setSelectedIds(new Set<string>())
      const failed = result.items.filter((item) => item.status === 'error')
      // reset_password 的临时口令只出现这一次：全成功也不自动关，
      // 由操作者点「完成」显式关闭并清空；其余动作仍以 toast 收口。
      if (failed.length === 0 && input.action !== 'reset_password') {
        setBatch(null)
        setBatchError(null)
        setBatchResult(null)
        notifySuccess(`批量操作完成：${result.items.length} 条成功`)
        return
      }
      // 部分失败或批量重置：弹窗保留，结果清单逐项列出。
      setBatchError(null)
      setBatchResult(result)
    },
    onError: (error) => {
      setBatchError(describeReauthError(error))
    },
  })
  const batchPending = batchMutation.isPending

  const openDetail = (user: User, opener: HTMLElement | null) => {
    setDetailUser(user)
    setDrawerReturnFocus(opener)
  }

  const rows = usersQuery.data?.items ?? []
  const error = usersQuery.isError ? describeApiError(usersQuery.error) : null
  const counts = usersQuery.data?.counts
  const selectable = allowed && !SHOWCASE
  const selectedRows = rows.filter((row) => selectedIds.has(row.id))
  const overBatchLimit = selectedRows.length > 10
  const allDeleted = selectedRows.length > 0 && selectedRows.every((row) => row.deleted_at !== null)
  // 角色/状态/邮箱是收窄条件；分组与已删除是视图，不是「筛选残留」。
  const narrowed = Boolean(urlState.role || urlState.status || urlState.q)
  const crossGroup = urlState.group ? nextGroupWithMembers(urlState.group, counts) : null

  const listStatus: 'loading' | 'error' | 'empty' | 'data' = usersQuery.isPending
    ? 'loading'
    : usersQuery.isError
      ? 'error'
      : rows.length === 0
        ? 'empty'
        : 'data'

  const emptyLabel = narrowed ? (
    <>
      <div>没有匹配的用户</div>
      <div className="app-muted">调整筛选条件后重试。</div>
      {/* 清除只撤收窄条件：分组与已删除是视图，不是筛选残留，原样保留。 */}
      {/* 触发按钮会随空态一起消失：焦点先交给常驻的搜索输入。 */}
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          onSearchChange(
            withFilter(urlState, { role: undefined, status: undefined, q: undefined }),
          )
          document.getElementById('user-filter-q')?.focus()
        }}
      >
        清除筛选
      </Button>
    </>
  ) : urlState.deleted ? (
    <>
      <div>没有已删除的用户</div>
      <Button variant="ghost" size="sm" onClick={() => onSearchChange(withDeleted(urlState, false))}>
        返回全部用户
      </Button>
    </>
  ) : urlState.group ? (
    <>
      <div>暂无{USER_GROUP_LABELS[urlState.group]}用户</div>
      {crossGroup ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onSearchChange(withGroup(urlState, crossGroup.group))}
        >
          {crossGroup.label}
        </Button>
      ) : null}
    </>
  ) : (
    <div>暂无用户</div>
  )

  if (!allowed) {
    return <DeniedCard eyebrow="Users" reason={DENY_REASON} />
  }

  const createReason = SHOWCASE ? READ_ONLY_REASON : null

  return (
    <>
      <PageHeader
        eyebrow="管理 · 用户"
        title={
          <>
            用户 <span className="accent">管理</span>
          </>
        }
        subtitle="按待处理/正式/空用户分组；分组计数不随筛选变化。"
        actions={
          <div className="app-page-actions">
            <BusyButton
              variant="primary"
              disabled={createReason !== null}
              title={createReason ?? undefined}
              onClick={(event) => {
                setCreateReturnFocus(event.currentTarget)
                setCreateOpen(true)
              }}
            >
              新建用户
            </BusyButton>
            {createReason ? <span className="app-deny-note">{createReason}</span> : null}
          </div>
        }
      />

      <Card eyebrow="Users" title="用户列表" action={<span className="app-caption">游标分页</span>}>
        <UserGroupTabs
          group={urlState.group}
          deleted={urlState.deleted}
          counts={counts}
          onGroupChange={(next) => onSearchChange(withGroup(urlState, next))}
        />
        <UserFilters
          values={urlState}
          qInput={qInput}
          deletedCount={counts?.deleted}
          // 筛选写 URL 一律 push：后退＝撤销最近一次筛选。
          onRoleChange={(next) => onSearchChange(withFilter(urlState, { role: next }))}
          onStatusChange={(next) => onSearchChange(withFilter(urlState, { status: next }))}
          onDeletedChange={(next) => onSearchChange(withDeleted(urlState, next))}
          onQInputChange={setQInput}
          onQCommit={commitQuery}
        />
        {selectable && selectedRows.length > 0 ? (
          <SelectionBar count={selectedRows.length} limit={10} onClear={() => setSelectedIds(new Set())}>
            <Button
              variant="ghost"
              size="sm"
              disabled={overBatchLimit || batchPending}
              title={overBatchLimit ? '一次最多 10 条' : undefined}
              onClick={() => openBatch('disable', selectedRows)}
            >
              停用
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={overBatchLimit || batchPending}
              title={overBatchLimit ? '一次最多 10 条' : undefined}
              onClick={() => openBatch('enable', selectedRows)}
            >
              启用
            </Button>
            <span className="app-batch-danger">
              <Button
                variant="outline-danger"
                size="sm"
                disabled={overBatchLimit || batchPending}
                title={overBatchLimit ? '一次最多 10 条' : undefined}
                onClick={() => openBatch('reset_password', selectedRows)}
              >
                重置口令
              </Button>
              <Button
                variant="outline-danger"
                size="sm"
                disabled={overBatchLimit || batchPending}
                title={overBatchLimit ? '一次最多 10 条' : undefined}
                onClick={() => openBatch('delete', selectedRows)}
              >
                删除
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={overBatchLimit || batchPending || !allDeleted}
                title={
                  overBatchLimit
                    ? '一次最多 10 条'
                    : allDeleted
                      ? undefined
                      : '仅已删除用户可恢复'
                }
                onClick={() => openBatch('restore', selectedRows)}
              >
                恢复
              </Button>
            </span>
          </SelectionBar>
        ) : null}
        <AsyncState
          status={listStatus}
          error={error ?? undefined}
          emptyLabel={emptyLabel}
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
            selectable={selectable}
            selectedIds={selectedIds}
            onToggleRow={toggleRow}
            onToggleAll={toggleAll}
            onPrevious={() => {
              const previous = goPrevious(stack)
              setStack(previous)
              const prev = currentCursor(previous)
              onSearchChange(
                prev
                  ? { ...urlState, cursor: prev }
                  : {
                      role: urlState.role,
                      status: urlState.status,
                      q: urlState.q,
                      group: urlState.group,
                      deleted: urlState.deleted,
                    },
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

      <UserBatchModal
        open={batch !== null}
        action={batch?.action ?? null}
        rows={batch?.rows ?? []}
        pending={batchPending}
        error={batchError}
        result={batchResult}
        onClose={closeBatch}
        onConfirm={(password) => {
          if (!batch) return
          setBatchError(null)
          setBatchResult(null)
          batchMutation.mutate({
            action: batch.action,
            ids: batch.rows.map((row) => row.id),
            password,
          })
        }}
      />
    </>
  )
}
