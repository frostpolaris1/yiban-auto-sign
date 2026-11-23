/**
 * **功能**
 * `/system#announcement` 全站公告发布与清空分区。
 *
 * **归属**
 * `features/system/announcement` 分区组件。
 *
 * **口径**
 * - 通过全角色端点 `GET /api/announcement` 读取当前线上公告与最近更新时间（`updated_at`）。
 * - 发布经 `PUT /api/admin/announcement`：单行、去除首尾空白、不超过 200 字，前端先拦换行与超长。
 * - 清空公告显式发送 `{ text: '' }`，`text` 置空且 `updated_at` 回到 `null`。
 * - 清空是广播级不可逆写（撤下后原正文不在库里）：按钮只开确认弹窗，
 *   弹窗里点「确认清空」才发请求，文案写明后果。
 * - 仅主管理员可发布或清空；`admin` 与展示模式置灰写控件并给出明确原因。
 */

import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { AsyncState } from '@/components/ui/AsyncState'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { ConfirmModal } from '@/components/ui/ConfirmModal'
import { TextAreaInput } from '@/components/ui/Input'
import { notifySuccess } from '@/components/ui/toast'
import {
  ANNOUNCEMENT_QUERY_KEY,
  getAnnouncement,
  updateAnnouncement,
} from '@/lib/api/announcement'
import { formatDateTime } from '@/lib/format'
import { showcaseAnnouncement } from '@/lib/showcase'
import { SHOWCASE } from '@/lib/showcase-mode'
import {
  ANNOUNCEMENT_MAX_CHARS,
  countAnnouncementChars,
  formatAnnouncementError,
  hasAnnouncementLineBreak,
  validateAnnouncementDraft,
} from './drafts'

type AnnouncementSectionProps = {
  canWrite: boolean
  readOnlyReason: string | null
}

export function AnnouncementSection({ canWrite, readOnlyReason }: AnnouncementSectionProps) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<string | null>(null)
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [confirmingClear, setConfirmingClear] = useState(false)
  const [clearOpener, setClearOpener] = useState<HTMLElement | null>(null)

  const query = useQuery({
    queryKey: ANNOUNCEMENT_QUERY_KEY,
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseAnnouncement()) : getAnnouncement()),
    staleTime: 30_000,
  })

  const mutation = useMutation({
    mutationFn: (text: string) => updateAnnouncement({ text }),
    onSuccess: (next, submittedText) => {
      setFieldError(null)
      setSubmitError(null)
      setDraft(null)
      setConfirmingClear(false)
      queryClient.setQueryData(ANNOUNCEMENT_QUERY_KEY, next)
      void queryClient.invalidateQueries({ queryKey: ANNOUNCEMENT_QUERY_KEY })
      notifySuccess(submittedText === '' ? '全站公告已清空' : '全站公告已发布')
    },
    onError: (err) => {
      setSubmitError(formatAnnouncementError(err))
    },
  })

  const value = draft ?? query.data?.text ?? ''
  const charCount = countAnnouncementChars(value)
  const asyncStatus = query.isPending ? 'loading' : query.isError ? 'error' : 'data'

  function handlePublish(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canWrite || mutation.isPending || query.data === undefined) return
    setSubmitError(null)
    const checked = validateAnnouncementDraft(value)
    if (!checked.valid) {
      setFieldError(checked.error)
      document.getElementById('announcement-text')?.focus()
      return
    }
    setFieldError(null)
    mutation.mutate(checked.text)
  }

  /** 清空只开确认弹窗：请求由弹窗的确认动作发出。 */
  function handleClear(opener: HTMLElement) {
    if (!canWrite || mutation.isPending || query.data === undefined) return
    setClearOpener(opener)
    setConfirmingClear(true)
  }

  function confirmClear() {
    setFieldError(null)
    setSubmitError(null)
    mutation.mutate('')
  }

  return (
    <section
      id="announcement"
      className="sys-section"
      tabIndex={-1}
      aria-labelledby="announcement-section-heading"
    >
      <div className="sys-section-head">
        <h2 id="announcement-section-heading" className="sys-section-title">
          全站公告
        </h2>
        <p className="sys-section-desc">
          向所有已登录用户展示置顶维护通知；单行文本上限 {ANNOUNCEMENT_MAX_CHARS} 字，支持随时清空。
        </p>
      </div>

      <Card eyebrow="Announcement" title="全站公告配置" className="sys-policy-card">
        <AsyncState
          status={asyncStatus}
          error={query.isError ? formatAnnouncementError(query.error) : undefined}
          onRetry={() => void query.refetch()}
        >
          {query.data ? (
            <>
              <div className="sys-announcement-preview" aria-live="polite">
                <div className="sys-announcement-meta">
                  <span>当前线上公告</span>
                  <span>
                    更新时间：
                    {query.data.updated_at ? formatDateTime(query.data.updated_at) : '未发布'}
                  </span>
                </div>
                <p className="sys-announcement-current">
                  {query.data.text ? query.data.text : '当前未发布全站公告'}
                </p>
              </div>

              <form
                className="app-form-stack"
                onSubmit={handlePublish}
                aria-labelledby="announcement-section-heading"
                noValidate
              >
                <TextAreaInput
                  id="announcement-text"
                  label="公告正文"
                  rows={2}
                  required
                  disabled={!canWrite || mutation.isPending}
                  value={value}
                  placeholder="输入单行公告内容，不超过 200 字…"
                  hint={`仅支持单行，最多 ${ANNOUNCEMENT_MAX_CHARS} 字（当前 ${charCount}/${ANNOUNCEMENT_MAX_CHARS}）`}
                  keepHint
                  error={fieldError ?? undefined}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                      e.preventDefault()
                      setFieldError('公告仅支持单行文本，不能包含换行')
                    }
                  }}
                  onChange={(e) => {
                    const next = e.target.value
                    setSubmitError(null)
                    setDraft(next)
                    if (hasAnnouncementLineBreak(next)) {
                      setFieldError('公告仅支持单行文本，不能包含换行')
                    } else if (countAnnouncementChars(next) > ANNOUNCEMENT_MAX_CHARS) {
                      setFieldError(`公告最多 ${ANNOUNCEMENT_MAX_CHARS} 个字符，请精简后再试`)
                    } else {
                      setFieldError(null)
                    }
                  }}
                />

                {/* 确认弹窗开着时失败文案只留在弹窗里，避免同一段话出现两次。 */}
                {submitError && !confirmingClear ? <Alert tone="danger">{submitError}</Alert> : null}

                <div className="sys-card-actions">
                  {readOnlyReason ? <span className="app-deny-note">{readOnlyReason}</span> : null}
                  <Button
                    type="button"
                    variant="outline-danger"
                    size="sm"
                    disabled={!canWrite || mutation.isPending || query.data.text === ''}
                    title={readOnlyReason ?? undefined}
                    onClick={(event) => handleClear(event.currentTarget)}
                  >
                    清空公告
                  </Button>
                  <BusyButton
                    type="submit"
                    variant="primary"
                    size="sm"
                    busy={mutation.isPending}
                    busyLabel="发布中…"
                    disabled={!canWrite}
                    title={readOnlyReason ?? undefined}
                  >
                    发布公告
                  </BusyButton>
                </div>
              </form>
            </>
          ) : null}
        </AsyncState>
      </Card>

      <ConfirmModal
        open={confirmingClear}
        title="清空全站公告？"
        description="清空后所有用户立即看不到当前公告，原正文不会保留在库里（未提交的输入框内容不受影响）。需要时可重新发布。"
        confirmText="确认清空"
        tone="danger"
        pending={mutation.isPending}
        error={confirmingClear ? submitError : null}
        returnFocusTo={clearOpener}
        onConfirm={confirmClear}
        onClose={() => {
          setConfirmingClear(false)
          setSubmitError(null)
        }}
      />
    </section>
  )
}
