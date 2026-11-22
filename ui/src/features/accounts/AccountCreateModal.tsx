/**
 * **功能**
 * 新建账号弹窗：master/admin 单页建号，含绑定用户（选填）。
 *
 * **归属**
 * `features/accounts` 展示层：建号走 `POST /api/accounts`。
 * 表单状态与校验走 react-hook-form＋zod（2026-10-29 天花板裁决 S2）；
 * 判据单一来源在 `schema.ts`，这里只保留请求体构造。
 *
 * **口径**
 * ticket13 起 user 不再进管理壳（AppShell 一律 302 用户端 `/app`），
 * 弹窗只剩 master/admin 单页形状：一个「绑定用户（选填）」邮箱字段，
 * 300ms 防抖查 lookup：命中带 id 提交，未注册给内联提示（不静默建号），
 * 留空＝自有。
 * 提交仍以服务端校验为准：lookup 只是预检，两步之间可被软删。
 * 学校列表可读（8v8①），下拉数据源在弹窗打开时取。
 * 必填判据失败是历史模态级单条文案（schema.ts 的常量），不移动焦点。
 */

import { useEffect, useState } from 'react'
import { useForm, type UseFormRegisterReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { FilterField } from '@/components/ui/FilterField'
import { Modal } from '@/components/ui/Modal'
import {
  createAccount,
  type Account,
  type AccountCreateBody,
} from '@/lib/api/account'
import { describeApiError } from '@/lib/api/client'
import { listAllSchools } from '@/lib/api/school'
import { lookupUsers } from '@/lib/api/user'
import { userStatusLabel } from '@/lib/identity'
import { ACCOUNT_CREATE_REQUIRED_TEXT, accountCreateSchema, type AccountCreateFormValues } from './schema'

const UNREGISTERED_TEXT = '该邮箱未注册，请先在用户管理建号'
const INCOMPLETE_EMAIL_TEXT = '请继续输入完整邮箱'

/**
 * 完整邮箱形状：恰好一个 @、两侧非空、无空白。
 * 与服务端 ValidEmail 同形（core/user，裁决 P3-5）：RFC 5321 允许无点域名，
 * UI 不得强加「域名含点」——ops@intranet 是合法登录名。
 * 掩码无法区分同形地址，故 exact 全等匹配才是绑定依据。
 */
function isCompleteEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+$/.test(value)
}

type AccountCreateModalProps = {
  open: boolean
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onCreated: (account: Account) => void
}

export function AccountCreateModal({
  open,
  returnFocusTo,
  onClose,
  onCreated,
}: AccountCreateModalProps) {
  return (
    <ManagerForm
      open={open}
      returnFocusTo={returnFocusTo}
      onClose={onClose}
      onCreated={onCreated}
    />
  )
}

/** 学校下拉的数据源。master/admin 均可读学校；失败留在弹窗内可重试。 */
function useSchoolOptions(open: boolean) {
  return useQuery({
    queryKey: ['schools', 'create'],
    queryFn: listAllSchools,
    enabled: open,
  })
}

/** 弹窗内共用的建号请求。错误留在弹窗里，输入不丢。 */
function useCreateMutation(onCreated: (account: Account) => void, onError: (text: string) => void) {
  return useMutation({
    mutationFn: (body: AccountCreateBody) => createAccount(body),
    onSuccess: onCreated,
    onError: (cause) => onError(describeApiError(cause)),
  })
}

type ShellProps = {
  open: boolean
  returnFocusTo: HTMLElement | null
  onClose: () => void
  children?: React.ReactNode
  footer?: React.ReactNode
  error: string | null
  title: string
  description: string
}

function CreateShell({ open, returnFocusTo, onClose, title, description, footer, children, error }: ShellProps) {
  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title={title}
      description={description}
      returnFocusTo={returnFocusTo}
      footer={footer}
      scrollBody
    >
      {children}
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

function SchoolSelect({
  registration,
  schools,
}: {
  registration: UseFormRegisterReturn
  schools: { id: string; name: string }[]
}) {
  return (
    <FilterField id="account-create-school" label="学校" required>
      <select className="select" {...registration}>
        <option value="">选择学校</option>
        {schools.map((school) => (
          <option key={school.id} value={school.id}>
            {school.name}
          </option>
        ))}
      </select>
    </FilterField>
  )
}

/** master/admin 单页：设备码与机型选填，绑定用户选填（8v8）。 */
function ManagerForm({
  open,
  returnFocusTo,
  onClose,
  onCreated,
}: {
  open: boolean
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onCreated: (account: Account) => void
}) {
  const [ownerEmail, setOwnerEmail] = useState('')
  const [debouncedEmail, setDebouncedEmail] = useState('')
  const [error, setError] = useState<string | null>(null)

  const { register, handleSubmit, reset } = useForm<AccountCreateFormValues>({
    resolver: zodResolver(accountCreateSchema),
    // 与现状一致：必填失败只有模态级文案，不把焦点搬去字段。
    shouldFocusError: false,
    defaultValues: {
      school_id: '',
      phone: '',
      name: '',
      password: '',
      device_code: '',
      phone_model: '',
    },
  })

  const create = useCreateMutation(onCreated, setError)

  const schools = useSchoolOptions(open)

  useEffect(() => {
    if (!open) {
      setOwnerEmail('')
      setDebouncedEmail('')
      setError(null)
      reset()
    }
  }, [open, reset])

  // 300ms 防抖：只在完整邮箱上查。exact=true 全等匹配（F1）：
  // 前缀＋掩码会把 a.one 与 a.two 绑成同一个人。
  // 邮箱的完整性与命中是异步预检，不走 schema（行为冻结，见 e2e ux2 master）。
  useEffect(() => {
    const trimmed = ownerEmail.trim()
    if (trimmed === debouncedEmail) return
    if (trimmed !== '' && !isCompleteEmail(trimmed)) {
      // 未完成不发请求：旧命中必须失效，matched 随键失效。
      setDebouncedEmail('')
      return
    }
    const timer = setTimeout(() => setDebouncedEmail(trimmed), 300)
    return () => clearTimeout(timer)
  }, [ownerEmail, debouncedEmail])

  const lookup = useQuery({
    queryKey: ['owner-lookup', debouncedEmail],
    queryFn: () => lookupUsers(debouncedEmail, { exact: true }),
    enabled: open && debouncedEmail.length > 0,
  })
  // 命中只在「输入与已查邮箱一致且查询已成功」时生效：
  // 防抖未完成时不得把「未查」当「未注册」，也不得用上一次的命中。
  const emailSettled = lookup.isSuccess && ownerEmail.trim() === debouncedEmail
  const matched = emailSettled ? (lookup.data?.items[0] ?? null) : null

  // 绑定用户填了却未命中（未注册、检索失败、防抖未停）时不许提交：
  // 否则三种情况都会静默落成「留空＝自有」，账号记到操作者名下。
  const ownerUnresolved = ownerEmail.trim() !== '' && matched === null

  const submit = handleSubmit(
    (values) => {
      if (ownerUnresolved) {
        setError('绑定用户未确认：命中已注册邮箱，或清空该字段')
        return
      }
      setError(null)
      const body: AccountCreateBody = {
        school_id: values.school_id,
        phone: values.phone.trim(),
        password: values.password,
      }
      if (values.name.trim() !== '') body.name = values.name.trim()
      const deviceCode = (values.device_code ?? '').trim()
      if (deviceCode !== '') body.device_code = deviceCode
      const phoneModel = (values.phone_model ?? '').trim()
      if (phoneModel !== '') body.phone_model = phoneModel
      // 走到这里 ownerEmail 必为空或已命中：留空＝自有，命中＝带 id。
      if (matched) body.owner_user_id = matched.id
      create.mutate(body)
    },
    () => setError(ACCOUNT_CREATE_REQUIRED_TEXT),
  )

  return (
    <CreateShell
      open={open}
      returnFocusTo={returnFocusTo}
      onClose={onClose}
      error={error}
      title="新建账号"
      description="单页登记；「绑定用户」留空即自有。"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={create.isPending}>
            取消
          </Button>
          <BusyButton
            variant="primary"
            busy={create.isPending}
            busyLabel="提交中…"
            disabled={ownerUnresolved}
            onClick={submit}
          >
            提交
          </BusyButton>
        </>
      }
    >
      <SchoolSelect registration={register('school_id')} schools={schools.data ?? []} />
      <FilterField id="account-create-phone" label="手机号" required>
        <input
          className="input"
          type="tel"
          inputMode="numeric"
          autoComplete="off"
          {...register('phone')}
        />
      </FilterField>
      <FilterField
        id="account-create-name-manager"
        label="备注名（选填）"
        hint="最多 50 字；留空显示为「账号+手机尾号」"
      >
        <input
          className="input"
          type="text"
          autoComplete="off"
          maxLength={50}
          {...register('name')}
        />
      </FilterField>
      <FilterField id="account-create-password" label="易班口令" required>
        <input
          className="input"
          type="password"
          autoComplete="new-password"
          {...register('password')}
        />
      </FilterField>
      <FilterField
        id="account-create-device-code"
        label="设备识别码（选填）"
        hint="留空时可在建号后从平台读回"
      >
        <input
          className="input"
          type="text"
          autoComplete="off"
          {...register('device_code')}
        />
      </FilterField>
      <FilterField id="account-create-phone-model-manager" label="机型（选填）">
        <input
          className="input"
          type="text"
          autoComplete="off"
          {...register('phone_model')}
        />
      </FilterField>
      <FilterField id="account-create-owner" label="绑定用户（选填）" hint="输入已注册邮箱">
        <input
          className="input"
          type="email"
          autoComplete="off"
          value={ownerEmail}
          onChange={(event) => setOwnerEmail(event.target.value)}
        />
      </FilterField>
      <div className="app-filter" aria-live="polite">
        {ownerEmail.trim() === '' ? (
          <span className="app-muted">默认归属自己</span>
        ) : !isCompleteEmail(ownerEmail.trim()) ? (
          // 不完整就不查：掩码无法区分同前缀地址，先补全。
          <span className="app-deny-note">{INCOMPLETE_EMAIL_TEXT}</span>
        ) : matched ? (
          <span className="app-owner-match">
            <Badge tone={matched.status === 'active' ? 'success' : 'warning'} dot>
              {userStatusLabel(matched.status)}
            </Badge>{' '}
            {matched.email_masked}
          </span>
        ) : lookup.isError ? (
          // 检索失败不是「未注册」：单独提示，可改后重试。
          <span className="app-deny-note">用户检索失败，请稍后重试</span>
        ) : lookup.isFetching || !emailSettled ? (
          <span className="app-muted">查询中…</span>
        ) : (
          <span className="app-deny-note">{UNREGISTERED_TEXT}</span>
        )}
      </div>
    </CreateShell>
  )
}
