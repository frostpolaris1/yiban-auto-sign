/**
 * **功能**
 * 用户时间偏好卡：读当前偏移，按 5 分钟步进改，或清除。
 *
 * **归属**
 * `features/schedule` 展示层：只挂在仪表盘，且只对 user 渲染。
 *
 * **口径**
 * 端点只对 user 开放：master/admin 不挂载本卡，也就不发请求。
 * `allowed=false` 是部署开关关闭：读数照常，控件禁用并说明原因。
 * 保存与清除都先经确认弹窗；确认前不发 PUT。
 * 请求体只含 offset_min；null 表示清除。
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { ConfirmModal } from '@/components/ui/ConfirmModal'
import { SelectInput } from '@/components/ui/Input'
import { ApiError } from '@/lib/api/client'
import { getTimePref, setTimePref, type TimePref } from '@/lib/api/schedule'
import {
  timePrefErrorText,
  timePrefLabel,
  timePrefOffsetError,
  timePrefOptions,
} from './drafts'

const TIME_PREF_KEY = ['time-pref'] as const

type PendingChange = { kind: 'set'; offset: number } | { kind: 'clear' }

export function TimePrefCard() {
  const queryClient = useQueryClient()
  const prefQuery = useQuery({
    queryKey: TIME_PREF_KEY,
    queryFn: getTimePref,
    staleTime: 0,
    retry: false,
  })

  const [draft, setDraft] = useState<string | null>(null)
  const [gateDenied, setGateDenied] = useState(false)
  const [pending, setPending] = useState<PendingChange | null>(null)
  const [returnFocus, setReturnFocus] = useState<HTMLElement | null>(null)
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [inlineError, setInlineError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const updateMutation = useMutation({
    mutationFn: setTimePref,
    onSuccess: (pref: TimePref) => {
      queryClient.setQueryData(TIME_PREF_KEY, pref)
      setPending(null)
      setConfirmError(null)
      setInlineError(null)
      setDraft(null)
      setNotice(
        pref.offset_min === null
          ? '已清除时间偏好'
          : `已保存时间偏好：${timePrefLabel(pref.offset_min)}`,
      )
    },
    onError: (error) => {
      setConfirmError(timePrefErrorText(error))
      if (error instanceof ApiError && error.status === 403) setGateDenied(true)
    },
  })

  const pref = prefQuery.data
  const stored = pref?.offset_min ?? null
  const storedValue = stored === null ? '' : String(stored)
  const currentValue = draft ?? storedValue
  const allowed = (pref?.allowed ?? true) && !gateDenied
  const busy = prefQuery.isPending || updateMutation.isPending
  const disabled = !allowed || busy
  const canSave = allowed && currentValue !== '' && currentValue !== storedValue
  const canClear = allowed && stored !== null

  const options = [{ value: '', label: '未设置' }, ...timePrefOptions()]

  const openSet = (opener: HTMLElement) => {
    const issue = timePrefOffsetError(currentValue)
    if (issue !== null) {
      setInlineError(issue)
      return
    }
    setInlineError(null)
    setConfirmError(null)
    setReturnFocus(opener)
    setPending({ kind: 'set', offset: Number(currentValue) })
  }

  const openClear = (opener: HTMLElement) => {
    setInlineError(null)
    setConfirmError(null)
    setReturnFocus(opener)
    setPending({ kind: 'clear' })
  }

  return (
    <Card
      className="col-6"
      eyebrow="Preferences"
      title="签到时间偏好"
      action={<span className="app-caption">仅本人可见</span>}
    >
      <p className="app-muted">签到时间相对有效窗口起点；步进 5 分钟，最多 120 分钟。</p>

      {prefQuery.isError ? (
        <div className="app-alert-block">
          <Alert tone="danger" title="读取失败">
            {timePrefErrorText(prefQuery.error)}
          </Alert>
        </div>
      ) : null}

      <dl className="app-detail-list">
        <dt>当前偏好</dt>
        <dd className="app-pref-current">
          {prefQuery.isPending ? '加载中…' : timePrefLabel(stored)}
        </dd>
      </dl>

      {pref && !pref.allowed ? (
        <div className="app-alert-block">
          <Alert tone="warning" title="功能未开放">
            此部署未开放时间偏好
          </Alert>
        </div>
      ) : null}

      <div className="app-time-pref-row">
        <SelectInput
          id="time-pref-offset"
          label="偏移分钟"
          options={options}
          value={currentValue}
          disabled={disabled}
          onChange={(event) => {
            setDraft(event.target.value)
            setInlineError(null)
            setNotice(null)
          }}
        />
        <Button
          variant="primary"
          disabled={disabled || !canSave}
          onClick={(event) => openSet(event.currentTarget)}
        >
          保存偏好
        </Button>
        <Button
          variant="ghost"
          disabled={disabled || !canClear}
          onClick={(event) => openClear(event.currentTarget)}
        >
          清除
        </Button>
      </div>

      {inlineError ? (
        <div className="app-alert-block">
          <Alert tone="danger" title="输入有误">
            {inlineError}
          </Alert>
        </div>
      ) : null}

      {notice ? (
        <div className="app-alert-block">
          <Alert tone="success" title="操作结果" dismissible>
            {notice}
          </Alert>
        </div>
      ) : null}

      <ConfirmModal
        open={pending !== null}
        title={pending?.kind === 'clear' ? '清除时间偏好？' : '保存时间偏好？'}
        description={
          pending?.kind === 'clear'
            ? '将清除已设置的时间偏好，恢复默认签到时间。'
            : pending !== null
              ? `将签到时间设为 ${timePrefLabel(pending.offset)}。`
              : undefined
        }
        confirmText={pending?.kind === 'clear' ? '确认清除' : '确认保存'}
        pending={updateMutation.isPending}
        error={confirmError}
        returnFocusTo={returnFocus}
        onClose={() => {
          setPending(null)
          setConfirmError(null)
        }}
        onConfirm={() => {
          if (pending === null) return
          setConfirmError(null)
          updateMutation.mutate(
            pending.kind === 'clear' ? { offset_min: null } : { offset_min: pending.offset },
          )
        }}
      />
    </Card>
  )
}
