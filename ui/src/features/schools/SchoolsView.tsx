/**
 * **功能**
 * 学校页装配：角色准入、游标、列表、详情抽屉与三个写操作模态。
 *
 * **归属**
 * `features/schools` 域容器：页面只引用它。
 *
 * **口径**
 * 角色来自 `GET /api/auth/me`。无 view 权限时不发学校列表请求，
 * 只渲染无权限卡片；导航项由 `nav.ts` 的角色元数据隐藏。
 * 页内动作无权限时置灰并给原因（admin 的编辑与管理员入口）。
 * 列表与管理员列表走 react-query；写操作成功后失效对应缓存。
 * 学校没有删除端点，页面不出删除入口。
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { PageHeader } from '@/components/ui/PageHeader'
import type { Role } from '@/lib/api/auth'
import { describeApiError } from '@/lib/api/client'
import {
  addSchoolAdmin,
  createSchool,
  listSchools,
  removeSchoolAdmin,
  updateSchool,
  type School,
  type SchoolAdmin,
  type SchoolUpdate,
} from '@/lib/api/school'
import { currentCursor, emptyCursorStack, goNext, goPrevious, hasPreviousPage } from '@/lib/cursor'
import { useMe } from '@/lib/session'
import { SchoolAdminRemoveModal } from './SchoolAdminRemoveModal'
import { SchoolAdminsModal } from './SchoolAdminsModal'
import { SchoolCreateModal } from './SchoolCreateModal'
import { SchoolDeniedCard } from './SchoolDeniedCard'
import { SchoolDetailDrawer } from './SchoolDetailDrawer'
import { SchoolEditModal } from './SchoolEditModal'
import { SchoolsTable } from './SchoolsTable'
import { buildSchoolCreate, type SchoolCreateDraft } from './drafts'
import { can, denyReason } from './permissions'

type EditInput = { id: string; body: SchoolUpdate }

export function SchoolsView() {
  const queryClient = useQueryClient()
  const meQuery = useMe()
  const role: Role | null = meQuery.data?.role ?? null
  const allowed = can(role, 'view')
  const createReason = denyReason(role, 'create')

  const [stack, setStack] = useState(emptyCursorStack)
  const cursor = currentCursor(stack)

  const schoolsQuery = useQuery({
    queryKey: ['schools-page', cursor ?? null],
    // 列表随时可变。缓存键回到旧游标时必须重新取数，不能给旧页。
    staleTime: 0,
    queryFn: () => listSchools({ cursor }),
    enabled: allowed,
  })

  const [detail, setDetail] = useState<School | null>(null)
  const [detailReturnFocus, setDetailReturnFocus] = useState<HTMLElement | null>(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [createReturnFocus, setCreateReturnFocus] = useState<HTMLElement | null>(null)
  const [createError, setCreateError] = useState<string | null>(null)

  const [editTarget, setEditTarget] = useState<School | null>(null)
  const [editReturnFocus, setEditReturnFocus] = useState<HTMLElement | null>(null)
  const [editError, setEditError] = useState<string | null>(null)

  const [adminsTarget, setAdminsTarget] = useState<School | null>(null)
  const [adminsReturnFocus, setAdminsReturnFocus] = useState<HTMLElement | null>(null)
  const [adminsError, setAdminsError] = useState<string | null>(null)

  const [removeTarget, setRemoveTarget] = useState<SchoolAdmin | null>(null)
  const [removeReturnFocus, setRemoveReturnFocus] = useState<HTMLElement | null>(null)
  const [removeError, setRemoveError] = useState<string | null>(null)

  const [notice, setNotice] = useState<{ id: number; text: string } | null>(null)

  /** 学校改名会影响账号页的学校下拉；两处缓存同批失效。 */
  const invalidateSchools = () => {
    void queryClient.invalidateQueries({ queryKey: ['schools-page'] })
    void queryClient.invalidateQueries({ queryKey: ['schools'] })
  }

  const createMutation = useMutation({
    mutationFn: (draft: SchoolCreateDraft): Promise<School> =>
      createSchool(buildSchoolCreate(draft)),
    onSuccess: (school) => {
      invalidateSchools()
      setStack(emptyCursorStack())
      setCreateOpen(false)
      setCreateError(null)
      setNotice({ id: Date.now(), text: `已新建 ${school.name}` })
    },
    onError: (error) => setCreateError(describeApiError(error)),
  })

  const editMutation = useMutation({
    mutationFn: (input: EditInput) => updateSchool(input.id, input.body),
    onSuccess: (school) => {
      invalidateSchools()
      setEditTarget(null)
      setEditError(null)
      // 抽屉可能开着同一所学校：用服务端响应替换快照。
      setDetail((previous) => (previous && previous.id === school.id ? school : previous))
      setNotice({ id: Date.now(), text: `已保存 ${school.name}` })
    },
    onError: (error) => setEditError(describeApiError(error)),
  })

  const addMutation = useMutation({
    mutationFn: (input: { schoolId: string; userId: string }) =>
      addSchoolAdmin(input.schoolId, { user_id: input.userId }),
    onSuccess: (_admin, input) => {
      void queryClient.invalidateQueries({ queryKey: ['school-admins', input.schoolId] })
      setAdminsError(null)
    },
    onError: (error) => setAdminsError(describeApiError(error)),
  })

  const removeMutation = useMutation({
    mutationFn: (input: { schoolId: string; uid: string }) =>
      removeSchoolAdmin(input.schoolId, input.uid),
    onSuccess: (_result, input) => {
      void queryClient.invalidateQueries({ queryKey: ['school-admins', input.schoolId] })
      setRemoveTarget(null)
      setRemoveError(null)
      setAdminsError(null)
    },
    onError: (error) => setRemoveError(describeApiError(error)),
  })

  const openDetail = (school: School, opener: HTMLElement | null) => {
    setDetail(school)
    setDetailReturnFocus(opener)
  }

  const openCreate = (opener: HTMLElement) => {
    createMutation.reset()
    setCreateError(null)
    setCreateReturnFocus(opener)
    setCreateOpen(true)
  }

  const openEdit = (school: School, opener: HTMLElement | null) => {
    editMutation.reset()
    setEditError(null)
    setEditReturnFocus(opener)
    setEditTarget(school)
  }

  const openAdmins = (school: School, opener: HTMLElement | null) => {
    addMutation.reset()
    removeMutation.reset()
    setAdminsError(null)
    setRemoveTarget(null)
    setRemoveError(null)
    setAdminsReturnFocus(opener)
    setAdminsTarget(school)
  }

  const openRemove = (admin: SchoolAdmin, opener: HTMLElement) => {
    removeMutation.reset()
    setRemoveError(null)
    setRemoveReturnFocus(opener)
    setRemoveTarget(admin)
  }

  const rows = schoolsQuery.data?.items ?? []
  const error = schoolsQuery.isError ? describeApiError(schoolsQuery.error) : null

  return (
    <>
      <PageHeader
        eyebrow="工作区 · 学校"
        title={
          <>
            学校 <span className="accent">列表</span>
          </>
        }
        subtitle="master 看全部学校；admin 只看绑定学校。点击学校名查看只读详情。"
        actions={
          <div className="app-page-actions">
            <Button
              variant="primary"
              disabled={createReason !== null}
              title={createReason ?? undefined}
              onClick={(event) => openCreate(event.currentTarget)}
            >
              新建学校
            </Button>
            {createReason ? <span className="app-deny-note">{createReason}</span> : null}
          </div>
        }
      />

      {notice ? (
        <div className="app-alert-block">
          <Alert key={notice.id} tone="success" title="操作结果" dismissible>
            {notice.text}
          </Alert>
        </div>
      ) : null}

      {!allowed ? (
        <SchoolDeniedCard reason={denyReason(role, 'view') ?? ''} />
      ) : (
        <Card
          eyebrow="Schools"
          title="学校列表"
          action={<span className="app-caption">游标分页</span>}
        >
          <SchoolsTable
            rows={rows}
            loading={schoolsQuery.isLoading || schoolsQuery.isFetching}
            error={error}
            role={role}
            pageIndex={stack.index}
            hasPrevious={hasPreviousPage(stack)}
            hasNext={Boolean(schoolsQuery.data?.next_cursor) && !schoolsQuery.isFetching}
            onPrevious={() => setStack(goPrevious)}
            onNext={() => {
              const next = schoolsQuery.data?.next_cursor
              if (!next) return
              setStack((previous) => goNext(previous, next))
            }}
            onRetry={() => {
              void schoolsQuery.refetch()
            }}
            onOpenDetail={openDetail}
            onEdit={openEdit}
            onAdmins={openAdmins}
          />
        </Card>
      )}

      <SchoolDetailDrawer
        school={detail}
        returnFocusTo={detailReturnFocus}
        onClose={() => setDetail(null)}
      />

      <SchoolCreateModal
        key={createOpen ? 'create-open' : 'create-closed'}
        open={createOpen}
        pending={createMutation.isPending}
        error={createError}
        returnFocusTo={createReturnFocus}
        onClose={() => {
          createMutation.reset()
          setCreateOpen(false)
          setCreateError(null)
        }}
        onConfirm={(draft) => {
          setCreateError(null)
          createMutation.mutate(draft)
        }}
      />

      <SchoolEditModal
        key={editTarget?.id ?? 'edit-closed'}
        school={editTarget}
        pending={editMutation.isPending}
        error={editError}
        returnFocusTo={editReturnFocus}
        onClose={() => {
          editMutation.reset()
          setEditTarget(null)
          setEditError(null)
        }}
        onConfirm={(body) => {
          if (!editTarget) return
          setEditError(null)
          editMutation.mutate({ id: editTarget.id, body })
        }}
      />

      <SchoolAdminsModal
        key={adminsTarget?.id ?? 'admins-closed'}
        school={adminsTarget}
        adding={addMutation.isPending}
        error={adminsError}
        returnFocusTo={adminsReturnFocus}
        onClose={() => {
          addMutation.reset()
          removeMutation.reset()
          setAdminsTarget(null)
          setAdminsError(null)
          setRemoveTarget(null)
          setRemoveError(null)
        }}
        onAdd={(userId) => {
          if (!adminsTarget) return
          setAdminsError(null)
          addMutation.mutate({ schoolId: adminsTarget.id, userId })
        }}
        onRemove={openRemove}
      />

      <SchoolAdminRemoveModal
        admin={removeTarget}
        pending={removeMutation.isPending}
        error={removeError}
        returnFocusTo={removeReturnFocus}
        onClose={() => {
          removeMutation.reset()
          setRemoveTarget(null)
          setRemoveError(null)
        }}
        onConfirm={() => {
          if (!adminsTarget || !removeTarget) return
          setRemoveError(null)
          removeMutation.mutate({ schoolId: adminsTarget.id, uid: removeTarget.user_id })
        }}
      />
    </>
  )
}
