/**
 * **功能**
 * 账号页装配：角色、学校、筛选、游标、列表、抽屉与写操作模态。
 *
 * **归属**
 * `features/accounts` 域容器：页面只引用它。
 *
 * **口径**
 * 筛选变更重置游标栈。手机号输入防抖 250ms。
 * 列表与详情走 react-query；写操作成功后失效两者的缓存。
 * 角色来自 `GET /api/auth/me`。取不到时一切写操作置灰并给原因。
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Card } from '@/components/ui/Card'
import { PageHeader } from '@/components/ui/PageHeader'
import {
  listAccounts,
  pauseAccount,
  reviewAccount,
  signAccount,
  verifyAccount,
  type Account,
  type AccountStatus,
  type AccountVerifyResult,
} from '@/lib/api/account'
import { fetchMe, type Role } from '@/lib/api/auth'
import { describeApiError } from '@/lib/api/client'
import { listAllSchools } from '@/lib/api/school'
import { AccountActionModal, type AccountActionState, type ReviewInput } from './AccountActionModal'
import { AccountDetailDrawer } from './AccountDetailDrawer'
import { AccountFilters, type AccountFilterValues } from './AccountFilters'
import { AccountsTable } from './AccountsTable'
import { currentCursor, emptyCursorStack, goNext, goPrevious, hasPreviousPage } from './cursor'
import { can, type AccountActionKind } from './permissions'

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
      return ''
  }
}

export function AccountsView() {
  const queryClient = useQueryClient()

  const meQuery = useQuery({ queryKey: ['me'], queryFn: fetchMe, retry: false })
  const role: Role | null = meQuery.data?.role ?? null

  const schoolsQuery = useQuery({
    queryKey: ['schools'],
    queryFn: listAllSchools,
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
      cursor ?? null,
    ],
    // 列表随时可变。缓存键回到旧筛选时必须重新取数，不能给旧页。
    staleTime: 0,
    queryFn: () =>
      listAccounts({
        status: filters.status,
        school_id: filters.schoolId,
        q: filters.q,
        cursor,
      }),
  })

  const [detailAccount, setDetailAccount] = useState<Account | null>(null)
  const [returnFocusTo, setReturnFocusTo] = useState<HTMLElement | null>(null)

  const [action, setAction] = useState<AccountActionState | null>(null)
  const [actionReturnFocus, setActionReturnFocus] = useState<HTMLElement | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [verifyResult, setVerifyResult] = useState<AccountVerifyResult | null>(null)
  const [notice, setNotice] = useState<{ id: number; text: string } | null>(null)

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
        setVerifyResult(result as AccountVerifyResult)
        return
      }
      setAction(null)
      setNotice({ id: Date.now(), text: successText(input.kind, input.account) })
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
      />

      {meQuery.isError ? (
        <div className="app-alert-block">
          <Alert tone="warning" title="会话未就绪">
            {describeApiError(meQuery.error)}。写操作已禁用。
          </Alert>
        </div>
      ) : null}

      {notice ? (
        <div className="app-alert-block">
          <Alert key={notice.id} tone="success" title="操作结果" dismissible>
            {notice.text}
          </Alert>
        </div>
      ) : null}

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
        />
      </Card>

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
    </>
  )
}
