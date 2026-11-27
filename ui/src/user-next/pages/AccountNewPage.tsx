/**
 * **功能**
 * 新建签到账号：识别人、确认学校、补照片、提交审核四拍。
 *
 * **归属**
 * user-next 页面层：只做编排与展示；平台协议在 lib/api。
 *
 * **口径**
 * 识别口令只存在内存；不写 URL、不写 storage。
 * 识别失败可手动选学校，绝不把失败伪装成「没有账号」。
 * 提交后只承诺「待审核」，不给通过时长。
 */

import { useRef, useState } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { notifySuccess } from '@/components/ui/toast'
import {
  createAccount,
  detectAccount,
  type AccountDetectResult,
} from '@/lib/api/account'
import { ApiError } from '@/lib/api/client'
import { photoBytesPath, uploadPhoto } from '@/lib/api/photo'
import type { Photo } from '@/lib/api/photo'
import { photoUploadErrorText } from '@/features/me/display'
import { ME_KEYS, useMyAccounts, useSchools } from '@/features/me/queries'

type Step = 1 | 2 | 3 | 'pending'

function detectErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '手机号或口令不正确，请检查后重试。'
    if (error.status === 403) return '当前状态不能识别账号。'
    if (error.status === 429) return '尝试过多，请稍后再试。'
    if (error.status === 502) return '平台暂时不可用；可以手动选择学校后继续。'
    if (error.status === 503) return '系统维护中；可以稍后重试或手动选择学校。'
    return '没有识别到账号，请检查手机号与口令。'
  }
  return '网络异常，请稍后重试。'
}

function createErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return '同校同号已有账号。如果刚删除过，请先到账号页恢复。'
    }
    if (error.status === 429) return '请求过多或容量不足，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    if (error.status === 403) return '当前状态不能创建账号。'
    if (error.status === 400) return '资料不完整，请回到上一步检查。'
    return '提交失败，请稍后再试。'
  }
  return '网络异常，请稍后重试。'
}

function detectedSchoolId(result: AccountDetectResult | null): string {
  return result?.school?.id ?? ''
}

const STEP_LABELS: Record<1 | 2 | 3, string> = {
  1: '连接',
  2: '确认',
  3: '照片',
}

export function AccountNewPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const accountsQuery = useMyAccounts()
  const schoolsQuery = useSchools()

  const [step, setStep] = useState<Step>(1)
  const [phone, setPhone] = useState('')
  const [password, setPassword] = useState('')
  const [detected, setDetected] = useState<AccountDetectResult | null>(null)
  const [schoolId, setSchoolId] = useState('')
  const [schoolQuery, setSchoolQuery] = useState('')
  const [manual, setManual] = useState(false)
  const [name, setName] = useState('')
  const [phoneModel, setPhoneModel] = useState('')
  const [photo, setPhoto] = useState<Photo | null>(null)
  const [photoBusy, setPhotoBusy] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)

  const schools = schoolsQuery.data ?? []
  const filteredSchools = schools.filter((school) => {
    const term = schoolQuery.trim().toLowerCase()
    if (term === '') return true
    return (
      school.name.toLowerCase().includes(term) ||
      school.code.toLowerCase().includes(term)
    )
  })
  const live =
    accountsQuery.data?.items.find((item) => item.deleted_at === null) ?? null

  const detectMutation = useMutation({
    mutationFn: () => detectAccount({ phone: phone.trim(), password }),
    onSuccess: (result) => {
      setDetected(result)
      setSchoolId(detectedSchoolId(result))
      setPhoneModel(result.device_model)
      setManual(result.school === null)
      setError(null)
      setStep(2)
    },
    onError: (err) => setError(detectErrorText(err)),
  })

  const createMutation = useMutation({
    mutationFn: () => {
      if (schoolId === '') throw new Error('missing school')
      return createAccount({
        school_id: schoolId,
        phone: phone.trim(),
        password,
        ...(name.trim() !== '' ? { name: name.trim() } : {}),
        ...(phoneModel.trim() !== ''
          ? { phone_model: phoneModel.trim() }
          : {}),
        confirm_defaults:
          detected !== null &&
          !manual &&
          schoolId === detectedSchoolId(detected) &&
          phoneModel.trim() === (detected.device_model ?? '').trim(),
      })
    },
    onSuccess: () => {
      setError(null)
      setStep('pending')
      notifySuccess('已提交审核')
      void queryClient.invalidateQueries({ queryKey: ME_KEYS.accounts })
    },
    onError: (err) => setError(createErrorText(err)),
  })

  const onPickPhoto = async (file: File) => {
    if (file.type !== 'image/png' && file.type !== 'image/jpeg') {
      setError('仅支持 PNG / JPEG 照片。')
      return
    }
    setError(null)
    setPhotoBusy(true)
    try {
      const uploaded = await uploadPhoto(file)
      setPhoto(uploaded)
      notifySuccess('照片已上传')
    } catch (err) {
      setError(photoUploadErrorText(err))
    } finally {
      setPhotoBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const validateCredentials = (): boolean => {
    if (phone.length !== 11) {
      setError('请输入 11 位易班手机号。')
      document.getElementById('uapp-new-phone')?.focus()
      return false
    }
    if (password === '') {
      setError('请输入易班登录口令。')
      document.getElementById('uapp-new-password')?.focus()
      return false
    }
    setError(null)
    return true
  }

  if (accountsQuery.isPending) {
    return (
      <div className="un-newacct" aria-busy="true">
        <div className="un-skeleton" style={{ width: 180, height: 36 }} />
        <div className="un-skeleton" style={{ width: '100%', height: 260, marginTop: 24 }} />
      </div>
    )
  }

  if (accountsQuery.isError) {
    return (
      <div className="un-newacct">
        <header className="un-newacct__head">
          <Link className="un-page-back" to="/app/me/account">
            <span aria-hidden="true">←</span> 返回账号
          </Link>
          <h1 className="un-newacct__title">添加签到账号</h1>
        </header>
        <div className="un-alert un-alert--danger" role="alert">
          账号列表读取失败；请重试后再判断是否已有账号。
        </div>
        <button
          type="button"
          className="un-btn un-btn--secondary"
          style={{ marginTop: 16 }}
          onClick={() => void accountsQuery.refetch()}
        >
          重试
        </button>
      </div>
    )
  }

  if (live !== null && step !== 'pending') {
    return (
      <div className="un-newacct">
        <header className="un-newacct__head">
          <Link className="un-page-back" to="/app/me/account">
            <span aria-hidden="true">←</span> 返回账号
          </Link>
          <h1 className="un-newacct__title">已经连接过一个账号</h1>
          <p className="un-newacct__lede">
            一人一号。要更换学校或手机号，请先到账号页处理现有账号，而不是再建一个。
          </p>
        </header>
        <div className="un-newacct__exists">
          <p className="un-newacct__exists-name">
            {live.name || live.phone_masked}
          </p>
          <p className="un-newacct__exists-meta">
            {live.phone_masked} · 创建于 {live.created_at.slice(0, 10)}
          </p>
          <div className="un-newacct__actions">
            <Link className="un-btn un-btn--primary" to="/app/me/account">
              查看现有账号
            </Link>
            <Link className="un-btn un-btn--ghost" to="/app/me">
              返回我的
            </Link>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="un-newacct">
      <header className="un-newacct__head">
        <Link className="un-page-back" to="/app/me/account">
          <span aria-hidden="true">←</span> 返回账号
        </Link>
        <h1 className="un-newacct__title">
          {step === 'pending'
            ? '已提交审核'
            : step === 1
              ? '添加签到账号'
              : step === 2
                ? '确认账号资料'
                : '补充现场照片'}
        </h1>
        <p className="un-newacct__lede">
          {step === 1
            ? '先用易班账号读取学校和设备。口令会加密保存，用于自动签到。'
            : step === 2
              ? '平台给出的资料只用于确认；你可以改备注名和机型。'
              : step === 3
                ? '照片用于签到审核，选填；之后也可以在账号页补。'
                : '审核通过后会自动开始签到。审核是人工的，不承诺固定时长。'}
        </p>
      </header>

      {step !== 'pending' ? (
        <ol className="un-steps" aria-label="新建账号步骤">
          {([1, 2, 3] as const).map((n) => {
            const state = n === step ? 'is-current' : n < step ? 'is-done' : ''
            return (
              <li
                key={n}
                className={'un-steps__item ' + state}
                aria-current={n === step ? 'step' : undefined}
              >
                <span className="un-steps__bar" aria-hidden="true" />
                <span className="un-steps__num">{n}</span>
                <span className="un-steps__label">{STEP_LABELS[n]}</span>
              </li>
            )
          })}
        </ol>
      ) : null}

      {error ? (
        <div className="un-alert un-alert--danger un-newacct__error" role="alert">
          {error}
        </div>
      ) : null}

      {step === 1 ? (
        <div className="un-newacct__grid">
          <form
            className="un-newacct__form"
            noValidate
            onSubmit={(event) => {
              event.preventDefault()
              if (!validateCredentials()) return
              detectMutation.mutate()
            }}
          >
            <div className="un-field">
              <label className="un-field__label" htmlFor="uapp-new-phone">
                易班手机号
              </label>
              <input
                id="uapp-new-phone"
                className="un-input"
                type="tel"
                inputMode="numeric"
                autoComplete="tel"
                value={phone}
                placeholder="13800000000"
                onChange={(event) =>
                  setPhone(event.target.value.replace(/\D/g, '').slice(0, 11))
                }
              />
              <p className="un-field__hint">11 位，和你在易班登录时一致。</p>
            </div>
            <div className="un-field">
              <label className="un-field__label" htmlFor="uapp-new-password">
                易班登录口令
              </label>
              <input
                id="uapp-new-password"
                className="un-input"
                type="password"
                autoComplete="new-password"
                value={password}
                placeholder="用于后续自动签到"
                onChange={(event) => setPassword(event.target.value)}
              />
              <p className="un-field__hint">
                加密保存，只用于自动签到；不会展示给管理员。
              </p>
            </div>
            <div className="un-newacct__actions">
              <button
                type="submit"
                className="un-btn un-btn--primary"
                disabled={detectMutation.isPending}
              >
                {detectMutation.isPending ? '识别中…' : '识别我的账号'}
              </button>
              <button
                type="button"
                className="un-btn un-btn--ghost"
                onClick={() => {
                  if (!validateCredentials()) return
                  setDetected(null)
                  setManual(true)
                  setSchoolId('')
                  setStep(2)
                }}
              >
                手动选择学校
              </button>
            </div>
          </form>

          <aside className="un-newacct__aside">
            <p className="un-newacct__aside-title">会发生什么</p>
            <ol className="un-newacct__list">
              <li>识别学校与机型；不读取签到记录。</li>
              <li>你确认资料；可以改备注名。</li>
              <li>提交后进入人工审核。</li>
            </ol>
            <p className="un-newacct__quiet">
              识别失败不会创建账号，也可以手动选学校。
            </p>
          </aside>
        </div>
      ) : null}

      {step === 2 ? (
        <div className="un-newacct__grid">
          <form
            className="un-newacct__form"
            noValidate
            onSubmit={(event) => {
              event.preventDefault()
              setError(null)
              if (schoolId === '') {
                setError('请选择学校。')
                return
              }
              setStep(3)
            }}
          >
            {detected ? (
              <div className="un-detected">
                <span className="un-detected__mark" aria-hidden="true">
                  {detected.person_name?.trim().slice(0, 1) || '易'}
                </span>
                <div>
                  <p className="un-detected__name">
                    {detected.person_name?.trim() || '平台账号'}
                  </p>
                  <p className="un-detected__meta">
                    {detected.university_name} · 平台识别结果
                  </p>
                </div>
              </div>
            ) : null}

            <div className="un-field">
              <label className="un-field__label" htmlFor="uapp-school-search">
                学校
              </label>
              {!manual && detected?.school ? (
                <div className="un-school-current">
                  <span>
                    <strong>{detected.school.name}</strong>
                    <small>平台识别</small>
                  </span>
                  <button
                    type="button"
                    className="un-linkbtn"
                    onClick={() => setManual(true)}
                  >
                    换一所
                  </button>
                </div>
              ) : (
                <>
                  <input
                    id="uapp-school-search"
                    className="un-input"
                    value={schoolQuery}
                    placeholder="搜索学校名或编码"
                    onChange={(event) => setSchoolQuery(event.target.value)}
                  />
                  {schoolsQuery.isPending ? (
                    <p className="un-field__hint">正在读取学校…</p>
                  ) : schoolsQuery.isError ? (
                    <p className="un-field__hint">
                      学校读取失败。
                      <button
                        type="button"
                        className="un-linkbtn"
                        onClick={() => void schoolsQuery.refetch()}
                      >
                        重试
                      </button>
                    </p>
                  ) : filteredSchools.length === 0 ? (
                    <p className="un-field__hint">
                      没有匹配的学校；请联系管理员。
                    </p>
                  ) : (
                    <div
                      className="un-school-list"
                      role="listbox"
                      aria-label="学校"
                    >
                      {filteredSchools.map((school) => (
                        <button
                          key={school.id}
                          type="button"
                          role="option"
                          aria-selected={schoolId === school.id}
                          className={
                            schoolId === school.id
                              ? 'un-school-option is-selected'
                              : 'un-school-option'
                          }
                          onClick={() => setSchoolId(school.id)}
                        >
                          <span className="un-school-option__name">
                            {school.name}
                          </span>
                          <span className="un-school-option__code">
                            {school.code}
                          </span>
                          <span className="un-school-option__mark" aria-hidden="true">
                            {schoolId === school.id ? '已选' : ''}
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>

            <div className="un-field">
              <label className="un-field__label" htmlFor="uapp-new-name">
                备注名（选填）
              </label>
              <input
                id="uapp-new-name"
                className="un-input"
                value={name}
                maxLength={50}
                placeholder="例如：宿舍账号"
                onChange={(event) => setName(event.target.value)}
              />
              <p className="un-field__hint">只影响你自己的界面显示。</p>
            </div>

            <div className="un-field">
              <label className="un-field__label" htmlFor="uapp-new-model">
                机型（选填）
              </label>
              <input
                id="uapp-new-model"
                className="un-input"
                value={phoneModel}
                maxLength={64}
                placeholder="平台未返回时填写"
                onChange={(event) => setPhoneModel(event.target.value)}
              />
              <p className="un-field__hint">
                机型不是设备码；设备码只能通过刷新设备读取。
              </p>
            </div>

            <div className="un-newacct__actions">
              <button
                type="button"
                className="un-btn un-btn--ghost"
                onClick={() => setStep(1)}
              >
                上一步
              </button>
              <button type="submit" className="un-btn un-btn--primary">
                继续
              </button>
            </div>
          </form>

          <aside className="un-newacct__aside">
            <p className="un-newacct__aside-title">这一页只确认什么</p>
            <p>
              学校决定签到归属；机型用于平台识别。两者都能在审核前改。
            </p>
            <p className="un-newacct__quiet">
              平台姓名只用于本次确认，不写入你的账号资料。
            </p>
          </aside>
        </div>
      ) : null}

      {step === 3 ? (
        <div className="un-newacct__grid">
          <div className="un-newacct__form">
            <input
              ref={fileRef}
              type="file"
              accept="image/png,image/jpeg"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void onPickPhoto(file)
              }}
            />
            <button
              type="button"
              className={
                photo
                  ? 'un-photo-pick has-photo'
                  : dragOver
                    ? 'un-photo-pick is-over'
                    : 'un-photo-pick'
              }
              disabled={photoBusy}
              onClick={() => fileRef.current?.click()}
              onDragOver={(event) => {
                event.preventDefault()
                setDragOver(true)
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(event) => {
                event.preventDefault()
                setDragOver(false)
                const file = event.dataTransfer.files?.[0]
                if (file) void onPickPhoto(file)
              }}
            >
              {photo ? (
                <>
                  <img
                    className="un-photo-pick__img"
                    src={photoBytesPath(photo.id)}
                    alt="已上传的现场照片"
                  />
                  <span className="un-photo-pick__label">重新选择照片</span>
                </>
              ) : (
                <>
                  <span className="un-photo-pick__frame" aria-hidden="true" />
                  <span className="un-photo-pick__label">
                    {photoBusy
                      ? '上传中…'
                      : dragOver
                        ? '松开以上传'
                        : '拖一张照片到这里，或点击选择'}
                  </span>
                  <span className="un-photo-pick__hint">
                    PNG / JPEG · 仅用于签到审核
                  </span>
                </>
              )}
            </button>
            <p className="un-newacct__quiet">
              照片由你和管理员可见；保留期在「我的」里调整。
            </p>

            <div className="un-newacct__actions">
              <button
                type="button"
                className="un-btn un-btn--ghost"
                onClick={() => setStep(2)}
                disabled={createMutation.isPending || photoBusy}
              >
                上一步
              </button>
              <button
                type="button"
                className="un-btn un-btn--primary"
                disabled={createMutation.isPending || photoBusy}
                onClick={() => {
                  setError(null)
                  createMutation.mutate()
                }}
              >
                {createMutation.isPending ? '提交中…' : '提交审核'}
              </button>
            </div>
            <button
              type="button"
              className="un-linkbtn"
              disabled={createMutation.isPending || photoBusy}
              onClick={() => {
                setError(null)
                createMutation.mutate()
              }}
            >
              跳过照片，直接提交
            </button>
          </div>

          <aside className="un-newacct__aside">
            <p className="un-newacct__aside-title">审核时看什么</p>
            <p>
              管理员会核对学校、手机号与现场照片；不会看到你的易班口令。
            </p>
            <p className="un-newacct__quiet">
              提交后仍可撤回吗？不能撤回；但可以在账号页删除，7 天内恢复。
            </p>
          </aside>
        </div>
      ) : null}

      {step === 'pending' ? (
        <div className="un-newacct__pending">
          <span className="un-newacct__pending-mark" aria-hidden="true">
            已
          </span>
          <div>
            <p className="un-newacct__pending-title">资料已经在审核队列里</p>
            <p className="un-newacct__pending-text">
              审核通过后会自动开始签到。你可以先去「我的」看看账号状态；
              不需要重复提交。
            </p>
            <div className="un-newacct__actions">
              <button
                type="button"
                className="un-btn un-btn--primary"
                onClick={() => void navigate({ to: '/app/me/account' })}
              >
                查看账号
              </button>
              <Link className="un-btn un-btn--ghost" to="/app">
                回到今天
              </Link>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
