/**
 * **功能**
 * 账号页装配：角色、学校、筛选（含已删除）、游标、列表、
 * 创建、恢复、抽屉与写操作模态。
 *
 * **归属**
 * `features/accounts` 域容器：页面只引用它。
 *
 * **口径**
 * 筛选变更重置游标栈。手机号输入防抖 250ms。
 * 列表与详情走 react-query；写操作成功后失效两者的缓存。
 * 写操作成功的反馈走 toast；错误反馈留在原模态内，可回看。
 * 角色来自 `GET /api/auth/me`。取不到时一切写操作置灰并给原因。
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { PageHeader } from '@/components/ui/PageHeader'
import { notifySuccess } from '@/components/ui/toast'
import {
  deleteAccount,
  listAccounts,
  pauseAccount,
  restoreAccount,
  reviewAccount,
  setAccountLocation,
  signAccount,
  updateAccount,
  verifyAccount,
  type Account,
  type AccountLocation,
  type AccountLocationUpdate,
  type AccountStatus,
  type AccountUpdate,
  type AccountVerifyResult,
} from '@/lib/api/account'
import type { Role } from '@/lib/api/auth'
import { describeApiError } from '@/lib/api/client'
import { listAllSchools } from '@/lib/api/school'
import { SHOWCASE, useMe } from '@/lib/session'
import { showcaseAccounts, showcaseAllSchools } from '@/lib/showcase'
import { AccountActionModal, type AccountActionState, type ReviewInput } from './AccountActionModal'
import { AccountCreateModal } from './AccountCreateModal'
import { AccountDeleteModal } from './AccountDeleteModal'
import { AccountDetailDrawer } from './AccountDetailDrawer'
import { AccountEditModal } from './AccountEditModal'
import { AccountFilters, type AccountFilterValues } from './AccountFilters'
import { AccountLocationModal } from './AccountLocationModal'
import { AccountsTable } from './AccountsTable'
import { currentCursor, emptyCursorStack, goNext, goPrevious, hasPreviousPage } from '@/lib/cursor'
import { buildAccountUpdate, buildLocationUpdate, type AccountUpdateDraft, type LocationDraft } from './drafts'
import { can, denyReason, type AccountActionKind } from './permissions'

type MutationInput = {
  account: Account
  kind: AccountActionKind
  review?: ReviewInput
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

/** 删除成功后行必消失：焦点直接落筛选输入，不让焦点掉到 body。 */
function focusAfterRowRemoval() {
  document.querySelector<HTMLElement>('[aria-label="手机号筛选"]')?.focus()
}

export function AccountsView() {
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
  const [qInput, setQInput] = useState('')
  const [stack, setStack] = useState(emptyCursorStack)
  const cursor = currentCursor(stack)

  useEffect(() => {
    const next = qInput.trim() || undefined
    if (next === filters.q) return
    const timer = setTimeout(() => {
      setFilters((previous) => ({ ...previous, q: next }))
      setStack(emptyCursorStack())
    }, 250)
    return () => clearTimeout(timer)
  }, [qInput, filters.q])

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
  })

  const [detailAccount, setDetailAccount] = useState<Account | null>(null)
  const [returnFocusTo, setReturnFocusTo] = useState<HTMLElement | null>(null)

  const [action, setAction] = useState<AccountActionState | null>(null)
  const [actionReturnFocus, setActionReturnFocus] = useState<HTMLElement | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [verifyResult, setVerifyResult] = useState<AccountVerifyResult | null>(null)
  const [editTarget, setEditTarget] = useState<Account | null>(null)
  const [editReturnFocus, setEditReturnFocus] = useState<HTMLElement | null>(null)
  const [editError, setEditError] = useState<string | null>(null)
  const [locationTarget, setLocationTarget] = useState<Account | null>(null)
  const [locationReturnFocus, setLocationReturnFocus] = useState<HTMLElement | null>(null)
  const [locationErrorText, setLocationErrorText] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  const [deleteReturnFocus, setDeleteReturnFocus] = useState<HTMLElement | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createReturnFocus, setCreateReturnFocus] = useState<HTMLElement | null>(null)
  const [restoreError, setRestoreError] = useState<string | null>(null)
  const createReason = denyReason(role, 'create')

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

  const shapeMutation = useMutation({
    mutationFn: async (input: {
      account: Account
      op: 'update' | 'location' | 'delete' | 'restore'
      body?: AccountUpdate | AccountLocationUpdate
    }) => {
      switch (input.op) {
        case 'update':
          return updateAccount(input.account.id, input.body as AccountUpdate)
        case 'location':
          return setAccountLocation(input.account.id, input.body as AccountLocationUpdate)
        case 'delete':
          return deleteAccount(input.account.id)
        case 'restore':
          return restoreAccount(input.account.id)
      }
    },
    onSuccess: (result, input) => {
      void queryClient.invalidateQueries({ queryKey: ['accounts'] })
      void queryClient.invalidateQueries({ queryKey: ['account', input.account.id] })
      if (input.op === 'delete') {
        // 行将随刷新消失：先清还原点，让模态卸载的 onCloseAutoFocus
        // 不把焦点抢回已注定删除的触发器，再直接落筛选输入。
        setDeleteReturnFocus(null)
        focusAfterRowRemoval()
        setDeleteTarget(null)
        notifySuccess(`已删除 ${input.account.phone_masked}`)
      }
      if (input.op === 'update') {
        setEditTarget(null)
        notifySuccess(`已保存 ${input.account.phone_masked}`)
      }
      if (input.op === 'restore') {
        notifySuccess(`已恢复 ${input.account.phone_masked}`)
      }
      if (input.op === 'location') {
        // 契约无读端点：成功响应进会话内缓存，供下次预填（专档 §七.4 口径）。
        // 该 key 无订阅者，默认 gcTime 5 分钟即回收——此处声明永不过期，
        // 文案「本会话内可预填」的承诺才成立。
        queryClient.setQueryDefaults(['account-location'], { gcTime: Infinity })
        queryClient.setQueryData(['account-location', input.account.id], result as AccountLocation)
        setLocationTarget(null)
        notifySuccess(`已保存 ${input.account.phone_masked} 的位置策略`)
      }
    },
    onError: (error, input) => {
      const text = describeApiError(error)
      if (input.op === 'update') setEditError(text)
      if (input.op === 'location') setLocationErrorText(text)
      if (input.op === 'delete') setDeleteError(text)
      if (input.op === 'restore') setRestoreError(text)
    },
  })

  const closeEdit = () => {
    shapeMutation.reset()
    setEditTarget(null)
    setEditError(null)
  }

  const closeLocation = () => {
    shapeMutation.reset()
    setLocationTarget(null)
    setLocationErrorText(null)
  }

  const closeDelete = () => {
    shapeMutation.reset()
    setDeleteTarget(null)
    setDeleteError(null)
  }

  const openRestore = (account: Account) => {
    setRestoreError(null)
    shapeMutation.mutate({ account, op: 'restore' })
  }

  const openCreate = (opener: HTMLElement | null) => {
    setCreateReturnFocus(opener)
    setCreateOpen(true)
  }

  const closeCreate = () => {
    setCreateOpen(false)
  }

  const rows = accountsQuery.data?.items ?? []
  const error = accountsQuery.isError ? describeApiError(accountsQuery.error) : null

  return (
    <>
      <PageHeader
        eyebrow="工作区 · 账号"
        title={
          <>
            账号 <span className="accent">列表</span>
          </>
        }
        subtitle="按学校、状态与手机号筛选。点击任意行查看只读详情。"
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

        {restoreError ? (
          <div className="app-alert-block">
            <Alert tone="danger" title="恢复失败">
              {restoreError} 行保持已删状态，可重试。
            </Alert>
          </div>
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

        <AccountsTable
          rows={rows}
          loading={accountsQuery.isLoading || accountsQuery.isFetching}
          error={error}
          role={role}
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
          notifySuccess(`已创建 ${account.phone_masked}`)
        }}
      />

      <AccountDetailDrawer
        account={detailAccount}
        schools={schools}
        returnFocusTo={returnFocusTo}
        onClose={() => setDetailAccount(null)}
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
        pending={shapeMutation.isPending && shapeMutation.variables?.op === 'update'}
        error={editError}
        returnFocusTo={editReturnFocus}
        onClose={closeEdit}
        onConfirm={(draft: AccountUpdateDraft) => {
          if (!editTarget) return
          setEditError(null)
          shapeMutation.mutate({
            account: editTarget,
            op: 'update',
            body: buildAccountUpdate(draft),
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

      <AccountDeleteModal
        account={deleteTarget}
        pending={shapeMutation.isPending && shapeMutation.variables?.op === 'delete'}
        error={deleteError}
        returnFocusTo={deleteReturnFocus}
        onClose={closeDelete}
        onConfirm={() => {
          if (!deleteTarget) return
          setDeleteError(null)
          shapeMutation.mutate({ account: deleteTarget, op: 'delete' })
        }}
      />
    </>
  )
}
