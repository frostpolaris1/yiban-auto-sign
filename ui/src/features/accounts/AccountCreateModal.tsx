/**
 * **功能**
 * 新建账号弹窗：普通用户两页向导，master/admin 单页含绑定用户。
 *
 * **归属**
 * `features/accounts` 展示层：建号走 `POST /api/accounts`，
 * 设备刷新走 `POST /api/accounts/{id}/device-refresh`，机型走 PATCH。
 *
 * **口径**
 * 双形状按会话角色分（8v8）：user 两页——第 1 页学校、手机号、易班口令；
 * 第 2 页设备码只读（自动刷新一次）＋可选机型，「跳过」不阻断完成。
 * master/admin 单页：多一个「绑定用户（选填）」邮箱字段，300ms 防抖查
 * lookup：命中带 id 提交，未注册给内联提示（不静默建号），留空＝自有。
 * 普通用户界面零邮箱字段：本组件按角色分支，user 分支不渲染任何邮箱输入。
 * 提交仍以服务端校验为准：lookup 只是预检，两步之间可被软删。
 * 学校列表三角色均可读（8v8①），下拉数据源在弹窗打开时取。
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { FilterField } from '@/components/ui/FilterField'
import { Modal } from '@/components/ui/Modal'
import {
  createAccount,
  refreshDevice,
  updateAccount,
  type Account,
  type AccountCreateBody,
  type AccountDevice,
} from '@/lib/api/account'
import { describeApiError } from '@/lib/api/client'
import { listAllSchools } from '@/lib/api/school'
import { lookupUsers } from '@/lib/api/user'
import type { Role } from '@/lib/api/auth'
import { AccountDeviceView } from './AccountDeviceView'
import { userStatusLabel } from '@/lib/identity'

type AccountCreateModalProps = {
  open: boolean
  role: Role | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onCreated: (account: Account) => void
}

const UNREGISTERED_TEXT = '该邮箱未注册，请先在用户管理建号'
const INCOMPLETE_EMAIL_TEXT = '请继续输入完整邮箱'

/**
 * 完整邮箱形状：恰好一个 @、两侧非空、无空白。
 * 与服务端 ValidEmail 同形（core/user，裁决 P3-5）：RFC 5321 允许无点域名，
 * UI 不得强加「域名含点」——ops@intranet 是合法登录名（审查 F3）。
 * 掩码无法区分同形地址，故 exact 全等匹配才是绑定依据。
 */
function isCompleteEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+$/.test(value)
}

export function AccountCreateModal({
  open,
  role,
  returnFocusTo,
  onClose,
  onCreated,
}: AccountCreateModalProps) {
  if (role === 'user') {
    return (
      <UserWizard
        open={open}
        returnFocusTo={returnFocusTo}
        onClose={onClose}
        onCreated={onCreated}
      />
    )
  }
  return (
    <ManagerForm
      open={open}
      returnFocusTo={returnFocusTo}
      onClose={onClose}
      onCreated={onCreated}
    />
  )
}

/** 学校下拉的数据源。三角色均可读学校；失败留在弹窗内可重试。 */
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

/** 普通用户两页向导（8v8：第 2 页可跳过，跳过后可在详情补录）。 */
function UserWizard({
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
  const [step, setStep] = useState<1 | 2>(1)
  const [schoolId, setSchoolId] = useState('')
  const [phone, setPhone] = useState('')
  const [password, setPassword] = useState('')
  const [phoneModel, setPhoneModel] = useState('')
  const [error, setError] = useState<string | null>(null)

  // 第 1 页成功不关弹窗：进第 2 页；onCreated 在跳过/完成关闭时才交回容器。
  const create = useMutation({
    mutationFn: (body: AccountCreateBody) => createAccount(body),
    onSuccess: () => {
      setError(null)
      setStep(2)
    },
    onError: (cause) => setError(describeApiError(cause)),
  })
  void onCreated

  const schools = useSchoolOptions(open)
  const createdAccount = create.data ?? null

  useEffect(() => {
    if (!open) {
      setStep(1)
      setSchoolId('')
      setPhone('')
      setPassword('')
      setPhoneModel('')
      setError(null)
    }
  }, [open])

  const submitPageOne = () => {
    if (!schoolId || !phone.trim() || !password) {
      setError('学校、手机号与易班口令都是必填')
      return
    }
    setError(null)
    create.mutate({ school_id: schoolId, phone: phone.trim(), password })
  }

  if (step === 2 && createdAccount) {
    return (
      <WizardStepTwo
        open={open}
        account={createdAccount}
        phoneModel={phoneModel}
        onPhoneModelChange={setPhoneModel}
        returnFocusTo={returnFocusTo}
        onClose={() => {
          onClose()
          onCreated(createdAccount)
        }}
      />
    )
  }

  return (
    <CreateShell
      open={open}
      returnFocusTo={returnFocusTo}
      onClose={onClose}
      error={error}
      title="新建账号"
      description="第 1 页共 2 页：先登记账号，下一步补设备信息。"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={create.isPending}>
            取消
          </Button>
          <BusyButton
            variant="primary"
            busy={create.isPending}
            busyLabel="提交中…"
            onClick={submitPageOne}
          >
            提交
          </BusyButton>
        </>
      }
    >
      <SchoolSelect schoolId={schoolId} onSchoolChange={setSchoolId} schools={schools.data ?? []} />
      <FilterField id="account-create-phone" label="手机号" required>
        <input
          className="input"
          type="tel"
          inputMode="numeric"
          autoComplete="off"
          value={phone}
          onChange={(event) => setPhone(event.target.value)}
        />
      </FilterField>
      <FilterField id="account-create-password" label="易班口令" required>
        <input
          className="input"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </FilterField>
    </CreateShell>
  )
}

function SchoolSelect({
  schoolId,
  onSchoolChange,
  schools,
}: {
  schoolId: string
  onSchoolChange: (id: string) => void
  schools: { id: string; name: string }[]
}) {
  return (
    <FilterField id="account-create-school" label="学校" required>
      <select
        className="select"
        value={schoolId}
        onChange={(event) => onSchoolChange(event.target.value)}
      >
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

/** 第 2 页：自动读一次设备，成功只读展示；失败不阻断；机型可选。 */
function WizardStepTwo({
  open,
  account,
  phoneModel,
  onPhoneModelChange,
  returnFocusTo,
  onClose,
}: {
  open: boolean
  account: Account
  phoneModel: string
  onPhoneModelChange: (value: string) => void
  returnFocusTo: HTMLElement | null
  onClose: () => void
}) {
  const [saveError, setSaveError] = useState<string | null>(null)

  const device = useMutation({
    mutationFn: () => refreshDevice(account.id),
  })
  useEffect(() => {
    if (open && !device.isPending && !device.data && !device.isError) {
      device.mutate()
    }
    // 只在进入第 2 页时自动读一次；重试由按钮触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const save = useMutation({
    mutationFn: () => updateAccount(account.id, { phone_model: phoneModel }),
    onSuccess: () => onClose(),
    onError: (cause) => setSaveError(describeApiError(cause)),
  })

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title="新建账号"
      description="第 2 页共 2 页：设备信息由系统补全，可跳过。"
      returnFocusTo={returnFocusTo}
      scrollBody
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={save.isPending}>
            跳过
          </Button>
          <BusyButton
            variant="primary"
            busy={save.isPending}
            busyLabel="保存中…"
            onClick={() => {
              setSaveError(null)
              if (phoneModel.trim() === '') {
                onClose()
                return
              }
              save.mutate()
            }}
          >
            完成
          </BusyButton>
        </>
      }
    >
      {device.isPending ? <p className="app-muted">正在从易班平台读取设备信息…</p> : null}
      {device.isError ? (
        <Alert tone="warning" title="设备信息读取失败">
          {describeApiError(device.error)} 可稍后在账号详情中补录。
        </Alert>
      ) : null}
      {device.data ? <AccountDeviceView device={device.data as AccountDevice} /> : null}

      <FilterField
        id="account-create-phone-model"
        label="机型（选填）"
        hint="留空则不保存机型"
      >
        <input
          className="input"
          type="text"
          autoComplete="off"
          value={phoneModel}
          onChange={(event) => onPhoneModelChange(event.target.value)}
        />
      </FilterField>
      {saveError ? (
        <div className="app-modal-error">
          <Alert tone="danger" title="保存失败">
            {saveError}
          </Alert>
        </div>
      ) : null}
    </Modal>
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
  const [schoolId, setSchoolId] = useState('')
  const [phone, setPhone] = useState('')
  const [password, setPassword] = useState('')
  const [deviceCode, setDeviceCode] = useState('')
  const [phoneModel, setPhoneModel] = useState('')
  const [ownerEmail, setOwnerEmail] = useState('')
  const [debouncedEmail, setDebouncedEmail] = useState('')
  const [error, setError] = useState<string | null>(null)

  const create = useCreateMutation(onCreated, setError)

  const schools = useSchoolOptions(open)

  useEffect(() => {
    if (!open) {
      setSchoolId('')
      setPhone('')
      setPassword('')
      setDeviceCode('')
      setPhoneModel('')
      setOwnerEmail('')
      setDebouncedEmail('')
      setError(null)
    }
  }, [open])

  // 300ms 防抖：只在完整邮箱上查。exact=true 全等匹配（F1）：
  // 前缀＋掩码会把 a.one 与 a.two 绑成同一个人。
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
  // 否则三种情况都会静默落成「留空＝自有」，账号记到操作者名下（审查 H1）。
  const ownerUnresolved = ownerEmail.trim() !== '' && matched === null

  const submit = () => {
    if (!schoolId || !phone.trim() || !password) {
      setError('学校、手机号与易班口令都是必填')
      return
    }
    if (ownerUnresolved) {
      setError('绑定用户未确认：命中已注册邮箱，或清空该字段')
      return
    }
    setError(null)
    const body: AccountCreateBody = {
      school_id: schoolId,
      phone: phone.trim(),
      password,
    }
    if (deviceCode.trim() !== '') body.device_code = deviceCode.trim()
    if (phoneModel.trim() !== '') body.phone_model = phoneModel.trim()
    // 走到这里 ownerEmail 必为空或已命中：留空＝自有，命中＝带 id。
    if (matched) body.owner_user_id = matched.id
    create.mutate(body)
  }

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
      <SchoolSelect schoolId={schoolId} onSchoolChange={setSchoolId} schools={schools.data ?? []} />
      <FilterField id="account-create-phone" label="手机号" required>
        <input
          className="input"
          type="tel"
          inputMode="numeric"
          autoComplete="off"
          value={phone}
          onChange={(event) => setPhone(event.target.value)}
        />
      </FilterField>
      <FilterField id="account-create-password" label="易班口令" required>
        <input
          className="input"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
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
          value={deviceCode}
          onChange={(event) => setDeviceCode(event.target.value)}
        />
      </FilterField>
      <FilterField id="account-create-phone-model-manager" label="机型（选填）">
        <input
          className="input"
          type="text"
          autoComplete="off"
          value={phoneModel}
          onChange={(event) => setPhoneModel(event.target.value)}
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
          // 不完整就不查：掩码无法区分同前缀地址，先补全（审查 F1）。
          <span className="app-deny-note">{INCOMPLETE_EMAIL_TEXT}</span>
        ) : matched ? (
          <span className="app-owner-match">
            <Badge tone={matched.status === 'active' ? 'success' : 'warning'} dot>
              {userStatusLabel(matched.status)}
            </Badge>{' '}
            {matched.email_masked}
          </span>
        ) : lookup.isError ? (
          // 检索失败不是「未注册」：单独提示，可改后重试（审查 H1）。
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
