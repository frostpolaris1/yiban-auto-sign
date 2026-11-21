/**
 * **功能**
 * 账号页装配：角色、学校、筛选（含已删除）、待审提示条、游标、列表、
 * 创建、恢复、抽屉、写操作模态、二次口令与批量动作。
 *
 * **归属**
 * `features/accounts` 域容器：页面只引用它。
 *
 * **口径**
 * 筛选变更重置游标栈；任一批量选择在筛选或翻页时清空。
 * 手机号输入防抖 250ms。列表与详情走 react-query；写操作成功后失效两者
 * 的缓存，并同时失效 `['account-summary']`（与看板第 5 卡同键）。
 * 待审提示条只对 master/admin 取数；仅当 pending+rejected>0 显示。
 * 危险写（删除、恢复、设备刷新、凭据编辑、批量）要求操作者二次口令；
 * 400/403/429 用统一文案，口令只存在于当前模态内。
 * 编辑账号总是带 `expected_updated_at`；409 时弹窗不关，
 * 给「载入最新」与「用我的版本重试」两条出路；重试发送的是当前表单值。
 * 批量动作一次最多 10 条；每项独立事务，部分失败回结果清单。
 * 列表每 10 秒条件轮询一次；模态或抽屉打开时暂停，展示模式不发请求。
 * 角色来自 `GET /api/auth/me`。取不到时一切写操作置灰并给原因。
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { PageHeader } from '@/components/ui/PageHeader'
import { ReauthDialog } from '@/components/ui/ReauthDialog'
import { SelectionBar } from '@/components/ui/SelectionBar'
import { notifySuccess } from '@/components/ui/toast'
import {
  batchAccounts,
  deleteAccount,
  getAccount,
  getAccountSummary,
  listAccounts,
  pauseAccount,
  restoreAccount,
  reviewAccount,
  setAccountLocation,
  signAccount,
  updateAccount,
  verifyAccount,
  type Account,
  type AccountBatchAction,
  type AccountBatchResult,
  type AccountLocation,
  type AccountLocationUpdate,
  type AccountStatus,
  type AccountUpdate,
  type AccountVerifyResult,
} from '@/lib/api/account'
import type { Role } from '@/lib/api/auth'
import { describeApiError, describeReauthError, isConflictError } from '@/lib/api/client'
import { listAllSchools } from '@/lib/api/school'
import { SHOWCASE, useMe } from '@/lib/session'
import {
  showcaseAccountSummary,
  showcaseAccounts,
  showcaseAllSchools,
} from '@/lib/showcase'
import { AccountActionModal, type AccountActionState, type ReviewInput } from './AccountActionModal'
import { AccountBatchModal } from './AccountBatchModal'
import { AccountCreateModal } from './AccountCreateModal'
import { AccountDetailDrawer } from './AccountDetailDrawer'
import { AccountEditModal } from './AccountEditModal'
import { AccountFilters, type AccountFilterValues } from './AccountFilters'
import { AccountLocationModal } from './AccountLocationModal'
import { AccountPendingBanner } from './AccountPendingBanner'
import { AccountsTable } from './AccountsTable'
import { currentCursor, emptyCursorStack, goNext, goPrevious, hasPreviousPage } from '@/lib/cursor'
import { LIST_POLL_INTERVAL_MS } from '@/lib/query'
import {
  buildAccountUpdate,
  buildLocationUpdate,
  type AccountUpdateDraft,
  type LocationDraft,
} from './drafts'
import { can, denyReason, type AccountActionKind } from './permissions'
import { exactMatchIds } from './search'
import { statusLabel } from './display'

/** 批量动作的中文名与确认文案由 `AccountBatchModal` 统一持有。 */

type MutationInput = {
  account: Account
  kind: AccountActionKind
  review?: ReviewInput
}

type ShapeMutationInput = {
  account: Account
  op: 'update' | 'location' | 'delete' | 'restore'
  body?: AccountUpdate | AccountLocationUpdate
  /** 删除与恢复的二次口令。 */
  password?: string
}

function successText(kind: AccountActionKind, account: Account): string {
  switch (kind) {
    case 'pause':
      return `已暂停 ${account.phone_masked}`
    case 'resume':
      return `已恢复 ${account.phone_masked}`
    case 'sign':
      return `已受理 ${account.phone_masked} 的签到任务`
    case 'review':
      return `已完成 ${account.phone_masked} 的评审`
    case 'verify':
      return `已验证 ${account.phone_masked}`
  }
}

/** 删除行与清除筛选都会拆掉焦点所在节点：焦点直接落筛选输入，不掉到 body。 */
function focusFilterInput() {
  document.querySelector<HTMLElement>('[aria-label="账号筛选"]')?.focus()
}

type AccountsViewProps = {
  /** 照片区由页面装配后传入；本功能不依赖照片功能。 */
  photoSection?: (account: Account) => ReactNode
  /** 全局搜索落地（任务书 06）：检索词进搜索框，沿用既有防抖与精确命中判定。 */
  initialQuery?: string
  /** 全局搜索落地：该行加入高亮集合，复用现有精确匹配样式。 */
  locateId?: string
}

export function AccountsView({ photoSection, initialQuery, locateId }: AccountsViewProps = {}) {
  const queryClient = useQueryClient()

  const meQuery = useMe()
  const role: Role | null = meQuery.data?.role ?? null

  const schoolsQuery = useQuery({
    queryKey: ['schools'],
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseAllSchools()) : listAllSchools()),
    enabled: can(role, 'schoolFilter'),
  })
  const schools = schoolsQuery.data ?? []

  const [filters, setFilters] = useState<AccountFilterValues>({})
  const [qInput, setQInput] = useState(initialQuery ?? '')

  // 同路由再次落地（从搜索跳搜索）不重挂载：查询串是新的事实源，搜索框同步。
  useEffect(() => {
    setQInput(initialQuery ?? '')
  }, [initialQuery])
  const [stack, setStack] = useState(emptyCursorStack)
  const cursor = currentCursor(stack)

  // 任一筛选生效即给空态一条「清除筛选」出路；全空时不给空按钮。
  const hasActiveFilters = Boolean(
    filters.status || filters.schoolId || filters.q || filters.includeDeleted,
  )
  const clearFilters = () => {
    setFilters({})
    setQInput('')
    setStack(emptyCursorStack())
    // 触发按钮会随空态一起消失：焦点先交给常驻的筛选输入。
    focusFilterInput()
  }

  useEffect(() => {
    const next = qInput.trim() || undefined
    if (next === filters.q) return
    const timer = setTimeout(() => {
      setFilters((previous) => ({ ...previous, q: next }))
      setStack(emptyCursorStack())
    }, 250)
    return () => clearTimeout(timer)
  }, [qInput, filters.q])

  // 待审提示条：只对 master/admin 取数（user 不发 summary 请求）。
  // 与看板第 5 卡共用缓存键 `['account-summary']`；读取失败不显示提示条。
  const isManager = role === 'master' || role === 'admin'
  const summaryQuery = useQuery({
    queryKey: ['account-summary'],
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseAccountSummary()) : getAccountSummary(),
    enabled: isManager,
    staleTime: 0,
    retry: false,
  })
  const summary = summaryQuery.data ?? null
  const showPendingBanner =
    isManager && summary !== null && summary.pending + summary.rejected > 0

  /** 「只看待审核」：与状态下拉同一写法，连带复位游标栈。 */
  const filterPending = () => {
    setFilters((previous) => ({ ...previous, status: 'pending' }))
    setStack(emptyCursorStack())
  }

  const [detailAccount, setDetailAccount] = useState<Account | null>(null)
  const [returnFocusTo, setReturnFocusTo] = useState<HTMLElement | null>(null)

  const [action, setAction] = useState<AccountActionState | null>(null)
  const [actionReturnFocus, setActionReturnFocus] = useState<HTMLElement | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [verifyResult, setVerifyResult] = useState<AccountVerifyResult | null>(null)
  const [editTarget, setEditTarget] = useState<Account | null>(null)
  const [editReturnFocus, setEditReturnFocus] = useState<HTMLElement | null>(null)
  const [editError, setEditError] = useState<string | null>(null)
  const [editConflict, setEditConflict] = useState(false)
  const [editConflictPending, setEditConflictPending] = useState(false)
  const [locationTarget, setLocationTarget] = useState<Account | null>(null)
  const [locationReturnFocus, setLocationReturnFocus] = useState<HTMLElement | null>(null)
  const [locationErrorText, setLocationErrorText] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  const [deleteReturnFocus, setDeleteReturnFocus] = useState<HTMLElement | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [restoreTarget, setRestoreTarget] = useState<Account | null>(null)
  const [restoreReturnFocus, setRestoreReturnFocus] = useState<HTMLElement | null>(null)
  const [restoreError, setRestoreError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createReturnFocus, setCreateReturnFocus] = useState<HTMLElement | null>(null)
  const createReason = denyReason(role, 'create')
  const batchReason = denyReason(role, 'batch')
  // 展示构建只读：批量条与选择列都不出现。
  const selectable = can(role, 'batch') && batchReason === null

  // 批量选择与批量确认/结果状态。
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(() => new Set<string>())
  const [batch, setBatch] = useState<{ action: AccountBatchAction; rows: Account[] } | null>(null)
  const [batchError, setBatchError] = useState<string | null>(null)
  const [batchResult, setBatchResult] = useState<AccountBatchResult | null>(null)

  const clearSelection = () => setSelectedIds(new Set<string>())

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

  // 编辑并发：expected_updated_at 用打开时的快照；重试时换成最新行，
  // 请求体永远取弹窗当前表单值，不重放提交过的旧草稿。
  const editBaselineRef = useRef<Account | null>(null)

  // 任一模态或抽屉打开时暂停轮询：后台刷新不得改写用户正在操作的页面。
  const modalOpen =
    createOpen ||
    detailAccount !== null ||
    action !== null ||
    editTarget !== null ||
    locationTarget !== null ||
    deleteTarget !== null ||
    restoreTarget !== null ||
    batch !== null

  const accountsQuery = useQuery({
    queryKey: [
      'accounts',
      filters.status ?? null,
      filters.schoolId ?? null,
      filters.q ?? null,
      filters.includeDeleted ?? false,
      cursor ?? null,
    ],
    // 列表随时可变。缓存键回到旧筛选时必须重新取数，不能给旧页。
    staleTime: 0,
    queryFn: () => {
      if (SHOWCASE) return Promise.resolve(showcaseAccounts())
      return listAccounts({
        status: filters.status,
        school_id: filters.schoolId,
        q: filters.q,
        include_deleted: filters.includeDeleted || undefined,
        cursor,
      })
    },
    // 每 10 秒轮询一次，让外部变更可见；展示模式与模态打开时零请求。
    refetchInterval: SHOWCASE || modalOpen ? false : LIST_POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
  })

  // 筛选或翻页都换一批行：选择不跨页、不跨视图。
  useEffect(() => {
    setSelectedIds(new Set<string>())
  }, [filters, cursor])

  const mutation = useMutation({
    mutationFn: async (input: MutationInput) => {
      switch (input.kind) {
        case 'pause':
          return pauseAccount(input.account.id, { paused: true })
        case 'resume':
          return pauseAccount(input.account.id, { paused: false })
        case 'sign':
          return signAccount(input.account.id)
        case 'verify':
          return verifyAccount(input.account.id)
        case 'review':
          if (!input.review) {
            throw new Error('review needs a decision')
          }
          return reviewAccount(input.account.id, input.review)
      }
    },
    onSuccess: (result, input) => {
      void queryClient.invalidateQueries({ queryKey: ['accounts'] })
      void queryClient.invalidateQueries({ queryKey: ['account', input.account.id] })
      // 审核会迁移状态：汇总计数与看板第 5 卡同键，一并失效。
      void queryClient.invalidateQueries({ queryKey: ['account-summary'] })
      setActionError(null)
      if (input.kind === 'verify') {
        const verify = result as AccountVerifyResult
        setVerifyResult(verify)
        // 平台拒绝是结论，不是成功：结论留在模态里，只有通过才发成功 toast。
        if (verify.ok) notifySuccess(successText('verify', input.account))
        return
      }
      setAction(null)
      notifySuccess(successText(input.kind, input.account))
    },
    onError: (error) => {
      setActionError(describeApiError(error))
    },
  })

  const openDetail = (account: Account, opener: HTMLElement | null) => {
    setDetailAccount(account)
    setReturnFocusTo(opener)
  }

  const openAction = (account: Account, kind: AccountActionKind, opener: HTMLElement) => {
    mutation.reset()
    setActionError(null)
    setVerifyResult(null)
    setActionReturnFocus(opener)
    setAction({ account, kind })
  }

  const closeAction = () => {
    mutation.reset()
    setAction(null)
    setActionError(null)
    setVerifyResult(null)
  }

  const openEdit = (account: Account, opener: HTMLElement | null) => {
    editBaselineRef.current = account
    setEditConflict(false)
    setEditError(null)
    setEditReturnFocus(opener)
    setEditTarget(account)
  }

  const openLocation = (account: Account, opener: HTMLElement | null) => {
    setLocationErrorText(null)
    setLocationReturnFocus(opener)
    setLocationTarget(account)
  }

  const openDelete = (account: Account, opener: HTMLElement | null) => {
    setDeleteError(null)
    setDeleteReturnFocus(opener)
    setDeleteTarget(account)
  }

  const openRestore = (account: Account, opener: HTMLElement | null) => {
    setRestoreError(null)
    setRestoreReturnFocus(opener)
    setRestoreTarget(account)
  }

  const shapeMutation = useMutation({
    mutationFn: async (input: ShapeMutationInput) => {
      switch (input.op) {
        case 'update':
          return updateAccount(input.account.id, input.body as AccountUpdate)
        case 'location':
          return setAccountLocation(input.account.id, input.body as AccountLocationUpdate)
        case 'delete':
          return deleteAccount(input.account.id, input.password ?? '')
        case 'restore':
          return restoreAccount(input.account.id, input.password ?? '')
      }
    },
    onSuccess: (result, input) => {
      void queryClient.invalidateQueries({ queryKey: ['accounts'] })
      void queryClient.invalidateQueries({ queryKey: ['account', input.account.id] })
      // 删除/恢复/重提都会改待处理计数：与看板第 5 卡同键，一并失效。
      void queryClient.invalidateQueries({ queryKey: ['account-summary'] })
      if (input.op === 'delete') {
        // 行将随刷新消失：先清还原点，让模态卸载的 onCloseAutoFocus
        // 不把焦点抢回已注定删除的触发器，再直接落筛选输入。
        setDeleteReturnFocus(null)
        setDeleteTarget(null)
        focusFilterInput()
        notifySuccess(`已删除 ${input.account.phone_masked}`)
      }
      if (input.op === 'update') {
        setEditConflict(false)
        setEditTarget(null)
        // 两种编辑会把账号送回待审核：被拒账号重提、改绑手机号。
        // 状态迁移以响应为准，不在前端猜字段。
        const updated = result as Account
        if (updated.status === 'pending' && input.account.status !== 'pending') {
          notifySuccess(`已重新提交 ${updated.phone_masked}，等待管理员审核`)
        } else {
          notifySuccess(`已保存 ${updated.phone_masked}`)
        }
      }
      if (input.op === 'restore') {
        setRestoreTarget(null)
        notifySuccess(`已恢复 ${input.account.phone_masked}`)
      }
      if (input.op === 'location') {
        // 契约无读端点：成功响应进会话内缓存，供下次预填。
        // 该 key 无订阅者，默认 gcTime 5 分钟即回收——此处声明永不过期，
        // 文案「本会话内可预填」的承诺才成立。
        queryClient.setQueryDefaults(['account-location'], { gcTime: Infinity })
        queryClient.setQueryData(['account-location', input.account.id], result as AccountLocation)
        setLocationTarget(null)
        notifySuccess(`已保存 ${input.account.phone_masked} 的位置策略`)
      }
    },
    onError: (error, input) => {
      if (input.op === 'update') {
        // 409 是并发结论：弹窗不关，改走顶部状态条的两条出路。
        if (isConflictError(error)) {
          setEditError(null)
          setEditConflict(true)
          return
        }
        setEditError(describeReauthError(error))
      }
      if (input.op === 'location') setLocationErrorText(describeApiError(error))
      if (input.op === 'delete') setDeleteError(describeReauthError(error))
      if (input.op === 'restore') setRestoreError(describeReauthError(error))
    },
  })

  const closeEdit = () => {
    shapeMutation.reset()
    editBaselineRef.current = null
    setEditConflict(false)
    setEditTarget(null)
    setEditError(null)
  }

  const closeLocation = () => {
    shapeMutation.reset()
    setLocationTarget(null)
    setLocationErrorText(null)
  }

  const closeDelete = () => {
    // 在途锁关：删除结果不能因为 X/Esc 而丢弃操作者看不到结论。
    if (shapeMutation.isPending && shapeMutation.variables?.op === 'delete') return
    shapeMutation.reset()
    setDeleteTarget(null)
    setDeleteError(null)
  }

  const closeRestore = () => {
    if (shapeMutation.isPending && shapeMutation.variables?.op === 'restore') return
    shapeMutation.reset()
    setRestoreTarget(null)
    setRestoreError(null)
  }

  /** 409 出路一：取最新行替换快照；表单随之重置为服务端版本。 */
  const loadLatestAccount = async () => {
    if (!editTarget) return
    setEditConflictPending(true)
    try {
      const latest = await getAccount(editTarget.id)
      editBaselineRef.current = latest
      setEditTarget(latest)
      setEditConflict(false)
      setEditError(null)
    } catch (error) {
      setEditError(describeApiError(error))
    } finally {
      setEditConflictPending(false)
    }
  }

  /** 409 出路二：取最新行只换基线；请求体取弹窗当前表单值。 */
  const retryMineAccount = async (draft: AccountUpdateDraft) => {
    if (!editTarget) return
    // 先按当前表单构造：冲突期间的编辑也要生效；非法草稿留在弹窗内报错。
    let body: AccountUpdate
    try {
      body = buildAccountUpdate(draft, editTarget.name)
    } catch (error) {
      setEditError(error instanceof Error ? error.message : '草稿校验失败')
      return
    }
    setEditConflictPending(true)
    try {
      const latest = await getAccount(editTarget.id)
      editBaselineRef.current = latest
      setEditConflict(false)
      setEditError(null)
      shapeMutation.mutate({
        account: editTarget,
        op: 'update',
        body: { ...body, expected_updated_at: latest.updated_at },
      })
    } catch (error) {
      setEditError(describeApiError(error))
    } finally {
      setEditConflictPending(false)
    }
  }

  const openCreate = (opener: HTMLElement | null) => {
    setCreateReturnFocus(opener)
    setCreateOpen(true)
  }

  const closeCreate = () => {
    setCreateOpen(false)
  }

  const rows = accountsQuery.data?.items ?? []
  const selectedRows = rows.filter((row) => selectedIds.has(row.id))
  const overBatchLimit = selectedRows.length > 10
  const allDeleted = selectedRows.length > 0 && selectedRows.every((row) => row.deleted_at != null)

  const openBatch = (action: AccountBatchAction) => {
    if (selectedRows.length === 0) return
    batchMutation.reset()
    setBatchError(null)
    setBatchResult(null)
    setBatch({ action, rows: selectedRows })
  }

  const closeBatch = () => {
    batchMutation.reset()
    setBatch(null)
    setBatchError(null)
    setBatchResult(null)
  }

  const batchMutation = useMutation({
    mutationFn: (input: { action: AccountBatchAction; ids: string[]; password: string; reason?: string }) =>
      batchAccounts({
        action: input.action,
        ids: input.ids,
        current_password: input.password,
        ...(input.reason ? { reason: input.reason } : {}),
      }),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ['accounts'] })
      void queryClient.invalidateQueries({ queryKey: ['account-summary'] })
      clearSelection()
      const failed = result.items.filter((item) => item.status === 'error')
      if (failed.length === 0) {
        setBatch(null)
        setBatchError(null)
        setBatchResult(null)
        notifySuccess(`批量操作完成：${result.items.length} 条成功`)
        return
      }
      // 部分失败：弹窗保留，结果清单逐项列出失败原因。
      setBatchError(null)
      setBatchResult(result)
    },
    onError: (error) => {
      setBatchError(describeReauthError(error))
    },
  })
  const batchPending = batchMutation.isPending

  const error = accountsQuery.isError ? describeApiError(accountsQuery.error) : null
  // 高亮只认已提交给服务端的 q：输入框还在防抖时不提前标。
  const highlightIds = exactMatchIds(rows, filters.q)
  // 全局搜索落地（任务书 06）：locate 行加入高亮集合，复用现有精确匹配样式。
  const markedIds =
    locateId && rows.some((row) => row.id === locateId)
      ? new Set([...highlightIds, locateId])
      : highlightIds

  return (
    <>
      <PageHeader
        eyebrow="管理 · 账号"
        title={
          <>
            账号 <span className="accent">列表</span>
          </>
        }
        subtitle="按学校、状态或备注名筛选；完整手机号/邮箱精确命中并高亮。"
        actions={
          <>
            {createReason ? <span className="app-deny-note">{createReason}</span> : null}
            <Button
              variant="primary"
              aria-label="新建账号"
              disabled={createReason !== null}
              title={createReason ?? undefined}
              onClick={(event: React.MouseEvent<HTMLButtonElement>) =>
                openCreate(event.currentTarget)
              }
            >
              新建账号
            </Button>
          </>
        }
      />

      <Card
        eyebrow="Accounts"
        title="账号列表"
        action={<span className="app-caption">游标分页</span>}
      >
        {schoolsQuery.isError ? (
          <div className="app-alert-block">
            <Alert tone="warning" title="学校列表加载失败">
              {describeApiError(schoolsQuery.error)}
            </Alert>
          </div>
        ) : null}

        {/* 待审提示条在筛选栏上方：有未处理行才出现，不占空态位置。 */}
        {showPendingBanner ? (
          <AccountPendingBanner
            pending={summary.pending}
            rejected={summary.rejected}
            onFilterPending={filterPending}
          />
        ) : null}

        <AccountFilters
          values={filters}
          qInput={qInput}
          role={role}
          schools={schools}
          onStatusChange={(status?: AccountStatus) => {
            setFilters((previous) => ({ ...previous, status }))
            setStack(emptyCursorStack())
          }}
          onSchoolChange={(schoolId?: string) => {
            setFilters((previous) => ({ ...previous, schoolId }))
            setStack(emptyCursorStack())
          }}
          onQInputChange={setQInput}
          onDeletedChange={(deleted: boolean) => {
            setFilters((previous) => ({ ...previous, includeDeleted: deleted || undefined }))
            setStack(emptyCursorStack())
          }}
        />

        {selectable && selectedRows.length > 0 ? (
          <SelectionBar count={selectedRows.length} limit={10} onClear={clearSelection}>
            <Button
              variant="ghost"
              size="sm"
              disabled={overBatchLimit || batchPending}
              title={overBatchLimit ? '一次最多 10 条' : undefined}
              onClick={() => openBatch('review_approve')}
            >
              审核通过
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={overBatchLimit || batchPending}
              title={overBatchLimit ? '一次最多 10 条' : undefined}
              onClick={() => openBatch('review_reject')}
            >
              驳回
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={overBatchLimit || batchPending}
              title={overBatchLimit ? '一次最多 10 条' : undefined}
              onClick={() => openBatch('pause')}
            >
              暂停
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={overBatchLimit || batchPending}
              title={overBatchLimit ? '一次最多 10 条' : undefined}
              onClick={() => openBatch('resume')}
            >
              恢复签到
            </Button>
            <span className="app-batch-danger">
              <Button
                variant="outline-danger"
                size="sm"
                disabled={overBatchLimit || batchPending}
                title={overBatchLimit ? '一次最多 10 条' : undefined}
                onClick={() => openBatch('delete')}
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
                      : '仅已删除账号可恢复账号'
                }
                onClick={() => openBatch('restore')}
              >
                恢复账号
              </Button>
            </span>
          </SelectionBar>
        ) : null}

        <AccountsTable
          rows={rows}
          loading={accountsQuery.isLoading || accountsQuery.isFetching}
          error={error}
          role={role}
          highlightIds={markedIds}
          hasActiveFilters={hasActiveFilters}
          onClearFilters={clearFilters}
          includeDeleted={filters.includeDeleted ?? false}
          selectable={selectable}
          selectedIds={selectedIds}
          onToggleRow={toggleRow}
          onToggleAll={toggleAll}
          pageIndex={stack.index}
          hasPrevious={hasPreviousPage(stack)}
          hasNext={Boolean(accountsQuery.data?.next_cursor) && !accountsQuery.isFetching}
          onPrevious={() => setStack(goPrevious)}
          onNext={() => {
            const next = accountsQuery.data?.next_cursor
            if (!next) return
            setStack((previous) => goNext(previous, next))
          }}
          onRetry={() => {
            void accountsQuery.refetch()
          }}
          onOpenDetail={openDetail}
          onAction={openAction}
          onEdit={openEdit}
          onLocation={openLocation}
          onDelete={openDelete}
          onRestore={openRestore}
        />
      </Card>

      <AccountCreateModal
        open={createOpen}
        role={role}
        returnFocusTo={createReturnFocus}
        onClose={closeCreate}
        onCreated={(account: Account) => {
          setCreateOpen(false)
          void queryClient.invalidateQueries({ queryKey: ['accounts'] })
          // 新号固定进 pending：提示条与看板第 5 卡的计数随之失效。
          void queryClient.invalidateQueries({ queryKey: ['account-summary'] })
          notifySuccess(`已创建 ${account.phone_masked}`)
        }}
      />

      <AccountDetailDrawer
        account={detailAccount}
        schools={schools}
        returnFocusTo={returnFocusTo}
        onClose={() => setDetailAccount(null)}
        photoSection={photoSection}
      />

      <AccountActionModal
        state={action}
        pending={mutation.isPending}
        error={actionError}
        verifyResult={verifyResult}
        returnFocusTo={actionReturnFocus}
        onClose={closeAction}
        onConfirm={(review) => {
          if (!action) return
          setActionError(null)
          mutation.mutate({ account: action.account, kind: action.kind, review })
        }}
      />

      <AccountEditModal
        account={editTarget}
        role={role}
        pending={shapeMutation.isPending && shapeMutation.variables?.op === 'update'}
        error={editError}
        conflict={editConflict}
        conflictPending={editConflictPending}
        returnFocusTo={editReturnFocus}
        onClose={closeEdit}
        onLoadLatest={() => {
          void loadLatestAccount()
        }}
        onRetryMine={(draft) => {
          void retryMineAccount(draft)
        }}
        onConfirm={(draft: AccountUpdateDraft) => {
          if (!editTarget) return
          setEditError(null)
          setEditConflict(false)
          shapeMutation.mutate({
            account: editTarget,
            op: 'update',
            body: {
              ...buildAccountUpdate(draft, editTarget.name),
              expected_updated_at: (editBaselineRef.current ?? editTarget).updated_at,
            },
          })
        }}
      />

      <AccountLocationModal
        account={locationTarget}
        pending={shapeMutation.isPending && shapeMutation.variables?.op === 'location'}
        error={locationErrorText}
        returnFocusTo={locationReturnFocus}
        onClose={closeLocation}
        onConfirm={(draft: LocationDraft) => {
          if (!locationTarget) return
          setLocationErrorText(null)
          shapeMutation.mutate({
            account: locationTarget,
            op: 'location',
            body: buildLocationUpdate(draft),
          })
        }}
      />

      <ReauthDialog
        open={deleteTarget !== null}
        title="删除账号？"
        description={
          deleteTarget
            ? `账号 ${deleteTarget.phone_masked} · 当前状态 ${statusLabel(deleteTarget.status)}`
            : undefined
        }
        confirmText="确认删除"
        danger
        pending={shapeMutation.isPending && shapeMutation.variables?.op === 'delete'}
        error={deleteError}
        returnFocusTo={deleteReturnFocus}
        onClose={closeDelete}
        onConfirm={(password) => {
          if (!deleteTarget) return
          setDeleteError(null)
          shapeMutation.mutate({ account: deleteTarget, op: 'delete', password })
        }}
      >
        <Alert tone="danger" title="软删除">
          删除后账号进入软删除状态，凭据将被级联隐藏。确认后本行从列表移除。
        </Alert>
      </ReauthDialog>

      <ReauthDialog
        open={restoreTarget !== null}
        title="恢复账号？"
        description={restoreTarget ? `账号 ${restoreTarget.phone_masked} · 已删除` : undefined}
        confirmText="确认恢复"
        pending={shapeMutation.isPending && shapeMutation.variables?.op === 'restore'}
        error={restoreError}
        returnFocusTo={restoreReturnFocus}
        onClose={closeRestore}
        onConfirm={(password) => {
          if (!restoreTarget) return
          setRestoreError(null)
          shapeMutation.mutate({ account: restoreTarget, op: 'restore', password })
        }}
      >
        <Alert tone="warning" title="恢复">
          恢复后账号保持删除前状态；同属主已有活跃账号时服务端会拒绝。
        </Alert>
      </ReauthDialog>

      <AccountBatchModal
        open={batch !== null}
        action={batch?.action ?? null}
        rows={batch?.rows ?? []}
        pending={batchPending}
        error={batchError}
        result={batchResult}
        onClose={closeBatch}
        onConfirm={(password, reason) => {
          if (!batch) return
          setBatchError(null)
          setBatchResult(null)
          batchMutation.mutate({
            action: batch.action,
            ids: batch.rows.map((row) => row.id),
            password,
            reason,
          })
        }}
      />
    </>
  )
}
