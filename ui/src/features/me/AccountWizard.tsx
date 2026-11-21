/**
 * **功能**
 * 新建账号向导：先验后选三步（连接 → 确认 → 照片）与待审核落地，模态承载。
 *
 * **归属**
 * `features/me` 域容器：取数、校验、提交与错误映射在此；账号页与 /new 深链共用。
 *
 * **口径**
 * 第 1 步只拿手机号 + 易班口令调 detect；口令只存在于内存，不落库。
 * detect 成功自动带出学校/机型/平台姓名；失败可回退手选学校。
 * 提交后明确「待审核」，不承诺立即签到，不给 ETA。
 * 短流程分步入模态（NN/g wizard chunking）：带进度、可退出、不遮蔽必需上下文。
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Icon } from '@/components/ui/Icon'
import { PasswordInput, TextInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import { notifySuccess } from '@/components/ui/toast'
import {
  createAccount,
  detectAccount,
  type AccountDetectResult,
} from '@/lib/api/account'
import { ApiError } from '@/lib/api/client'
import { photoBytesPath, uploadPhoto } from '@/lib/api/photo'
import type { Photo } from '@/lib/api/photo'
import { photoUploadErrorText } from './display'
import { ME_KEYS, useSchools } from './queries'

type Step = 1 | 2 | 3 | 'pending'

function detectErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '请填写正确的手机号与易班口令。'
    if (error.status === 403) return '没有权限执行这个操作。'
    if (error.status === 429) return '尝试过多，请稍后再试。'
    if (error.status === 502) return '平台暂时不可用，请稍后再试或手动选学校。'
    if (error.status === 503) return '系统维护中，请稍后再试或手动选学校。'
    return '没有找到账号，请检查手机号与口令。'
  }
  return '网络异常，请稍后重试。'
}

function createErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return '同校同号已有账号。如果是你删除的账号，请先在账号页恢复。'
    }
    if (error.status === 429) return '请求过多或容量不足，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    if (error.status === 403) return '当前状态不能创建账号。'
    if (error.status === 400) return '资料不完整，请返回上一步检查。'
    return '提交失败，请稍后再试。'
  }
  return '网络异常，请稍后重试。'
}

function detectSchoolId(result: AccountDetectResult | null): string {
  return result?.school?.id ?? ''
}

export function AccountWizard({
  open,
  onOpenChange,
  returnFocusTo,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  returnFocusTo?: HTMLElement | null
}) {
  const queryClient = useQueryClient()
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
  const detectReturnFocus = useRef<HTMLElement | null>(null)

  const schools = schoolsQuery.data ?? []
  const filteredSchools = schools.filter((school) => {
    const term = schoolQuery.trim().toLowerCase()
    if (term === '') return true
    return (
      school.name.toLowerCase().includes(term) ||
      school.code.toLowerCase().includes(term)
    )
  })

  const detectMutation = useMutation({
    mutationFn: () => detectAccount({ phone: phone.trim(), password }),
    onSuccess: (result) => {
      setDetected(result)
      setSchoolId(detectSchoolId(result))
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
        ...(phoneModel.trim() !== '' ? { phone_model: phoneModel.trim() } : {}),
        // 契约：所有自动默认值都未改才可声明 true；机型也算默认值。
        confirm_defaults:
          detected !== null &&
          !manual &&
          schoolId === detectSchoolId(detected) &&
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

  const validateStep1Credentials = (): boolean => {
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

  const title =
    step === 'pending'
      ? '已提交审核'
      : step === 1
        ? '连接你的易班'
        : step === 2
          ? '确认账号资料'
          : '添加现场照片'

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      wide
      scrollBody
      returnFocusTo={returnFocusTo ?? detectReturnFocus.current}
      description={
        step === 1
          ? '先识别，再确认；口令只用于本次识别。'
          : step === 2
            ? '平台姓名只用于本次确认，不保存。'
            : step === 3
              ? '选填；可以先跳过，之后在账号页补。'
              : undefined
      }
      footer={
        step === 'pending' ? (
          <button
            type="button"
            className="uapp-button uapp-button--primary uapp-button--grow"
            onClick={() => onOpenChange(false)}
          >
            查看账号
          </button>
        ) : step === 1 ? (
          <button
            type="button"
            className="uapp-button uapp-button--primary uapp-button--grow"
            disabled={detectMutation.isPending}
            onClick={(event) => {
              detectReturnFocus.current = event.currentTarget
              if (!validateStep1Credentials()) return
              detectMutation.mutate()
            }}
          >
            {detectMutation.isPending ? '识别中…' : '识别我的账号'}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="uapp-button uapp-button--ghost"
              onClick={() => setStep(step === 2 ? 1 : 2)}
            >
              上一步
            </button>
            <button
              type="button"
              className="uapp-button uapp-button--primary uapp-button--grow"
              disabled={
                step === 2
                  ? schoolId === ''
                  : createMutation.isPending || photoBusy
              }
              onClick={() => {
                setError(null)
                if (step === 2) setStep(3)
                else createMutation.mutate()
              }}
            >
              {step === 2 ? '继续' : createMutation.isPending ? '提交中…' : '提交审核'}
            </button>
          </>
        )
      }
    >
      {step !== 'pending' ? (
        <div className="uapp-steps" aria-label="新建账号步骤">
          {([1, 2, 3] as const).map((n) => (
            <span
              key={n}
              className={
                n === step ? 'uapp-step uapp-step--active' : n < step ? 'uapp-step uapp-step--done' : 'uapp-step'
              }
              aria-current={n === step ? 'step' : undefined}
            >
              <span className="uapp-step-bar" aria-hidden="true" />
              <span className="uapp-step-label">
                {n} {n === 1 ? '连接' : n === 2 ? '确认' : '照片'}
              </span>
            </span>
          ))}
        </div>
      ) : null}

      {error ? (
        <div className="uapp-alert-block">
          <Alert tone="danger">{error}</Alert>
        </div>
      ) : null}

      <div className="uapp-step-pane" key={step}>
        {step === 1 ? (
          <>
            <div className="uapp-field">
              <TextInput
                id="uapp-new-phone"
                label="易班手机号"
                type="tel"
                inputMode="numeric"
                value={phone}
                placeholder="13800000000"
                onChange={(event) => setPhone(event.target.value.replace(/\D/g, '').slice(0, 11))}
              />
            </div>
            <div className="uapp-field">
              <PasswordInput
                id="uapp-new-password"
                label="易班登录口令"
                value={password}
                placeholder="只用于本次识别，不会保存"
                autoComplete="new-password"
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>
            <p className="uapp-card-sub">
              口令只用于一次认证，用来读取你的学校和设备；不会保存、不会展示给管理员。
            </p>
            <button
              type="button"
              className="uapp-hero-link"
              onClick={() => {
                if (!validateStep1Credentials()) return
                setDetected(null)
                setManual(true)
                setSchoolId('')
                setStep(2)
              }}
            >
              手动选择学校
            </button>
          </>
        ) : null}

        {step === 2 ? (
          <>
            {detected ? (
              <section className="uapp-hero uapp-hero--info">
                <div className="uapp-hero-identity">
                  <span className="uapp-row-icon">
                    <Icon name="user" size={18} />
                  </span>
                  <div className="uapp-hero-identity-text">
                    <h2 className="uapp-hero-name">
                      {detected.person_name || '平台账号'}
                    </h2>
                    <p className="uapp-hero-meta">{detected.university_name}</p>
                  </div>
                </div>
                <div className="uapp-rows">
                  <div className="uapp-row">
                    <span className="uapp-row-main">
                      <span className="uapp-row-label">机型</span>
                      <span className="uapp-row-sub">
                        {detected.device_model || '平台未返回'}
                      </span>
                    </span>
                  </div>
                  <div className="uapp-row">
                    <span className="uapp-row-main">
                      <span className="uapp-row-label">设备码</span>
                      <span className="uapp-row-sub">
                        {detected.device_code_present
                          ? '平台已有设备码；此处不展示完整内容'
                          : '平台未返回设备码'}
                      </span>
                    </span>
                  </div>
                </div>
              </section>
            ) : null}

            <div className="uapp-card-head uapp-wizard-head">
              <h2 className="uapp-card-title">学校</h2>
              {!manual && detected?.school ? (
                <button
                  type="button"
                  className="uapp-card-action"
                  onClick={() => setManual(true)}
                >
                  换一所
                </button>
              ) : null}
            </div>

            {manual || !detected?.school ? (
              <>
                <div className="uapp-field">
                  <TextInput
                    id="uapp-school-search"
                    label="搜索学校"
                    value={schoolQuery}
                    placeholder="输入学校名或编码"
                    onChange={(event) => setSchoolQuery(event.target.value)}
                  />
                </div>
                {schoolsQuery.isPending ? (
                  <p className="uapp-card-sub">正在读取学校…</p>
                ) : schoolsQuery.isError ? (
                  <div>
                    <p className="uapp-card-sub">学校读取失败。</p>
                    <button
                      type="button"
                      className="uapp-card-action"
                      onClick={() => void schoolsQuery.refetch()}
                    >
                      重试
                    </button>
                  </div>
                ) : filteredSchools.length === 0 ? (
                  <p className="uapp-card-sub">没有匹配的学校；请联系管理员。</p>
                ) : (
                  <div className="uapp-school-list" role="listbox" aria-label="学校">
                    {filteredSchools.map((school) => (
                      <button
                        key={school.id}
                        type="button"
                        role="option"
                        aria-selected={schoolId === school.id}
                        className={
                          schoolId === school.id
                            ? 'uapp-school-option active'
                            : 'uapp-school-option'
                        }
                        onClick={() => setSchoolId(school.id)}
                      >
                        <span className="uapp-school-option-text">
                          <span>{school.name}</span>
                          <span className="uapp-row-sub">{school.code}</span>
                        </span>
                        {schoolId === school.id ? <Icon name="check" size={16} /> : null}
                      </button>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <div className="uapp-row">
                <span className="uapp-row-main">
                  <span className="uapp-row-label">{detected.school.name}</span>
                  <span className="uapp-row-sub">平台识别结果</span>
                </span>
                <Icon name="check" size={16} />
              </div>
            )}

            <div className="uapp-field">
              <TextInput
                id="uapp-new-name"
                label="备注名（选填）"
                value={name}
                maxLength={50}
                placeholder="例如：宿舍账号"
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div className="uapp-field">
              <TextInput
                id="uapp-new-model"
                label="机型（选填）"
                value={phoneModel}
                maxLength={64}
                hint="机型不是设备码；设备码只能通过刷新设备读取"
                onChange={(event) => setPhoneModel(event.target.value)}
              />
            </div>
          </>
        ) : null}

        {step === 3 ? (
          <>
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
                  ? 'uapp-photo-picker has-photo'
                  : dragOver
                    ? 'uapp-photo-picker dragover'
                    : 'uapp-photo-picker'
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
                <span className="uapp-photo-preview">
                  <img src={photoBytesPath(photo.id)} alt="已上传的现场照片" />
                  <span className="uapp-row-label">照片已上传，可重新选择</span>
                </span>
              ) : (
                <>
                  <Icon name="camera" size={22} />
                  <span>{photoBusy ? '上传中…' : dragOver ? '松开以上传' : '拖拽照片到这里，或点击选择'}</span>
                  <span className="uapp-row-sub">PNG / JPEG · 最多 10 MB</span>
                </>
              )}
            </button>
            <p className="uapp-card-sub">
              仅用于签到审核；你和管理员可查看。
            </p>
            <button
              type="button"
              className="uapp-hero-link"
              disabled={createMutation.isPending || photoBusy}
              onClick={() => {
                setError(null)
                createMutation.mutate()
              }}
            >
              跳过照片并提交 ›
            </button>
          </>
        ) : null}

        {step === 'pending' ? (
          <div className="uapp-wizard-success">
            <span className="uapp-pending-check">
              <Icon name="check" size={22} />
            </span>
            <p>
              审核通过后会自动开始签到；你可以在账号页修改资料。
              <br />
              审核是人工的；不承诺固定时长。
            </p>
          </div>
        ) : null}
      </div>
    </Modal>
  )
}