/**
 * **功能**
 * 签到时间偏好弹窗：有效窗口、5 分钟档、拥挤度、保存与恢复自动。
 *
 * **归属**
 * `features/me` 域容器：取数、选择器与保存在此；设置行与深链共用。
 *
 * **口径**
 * 必须显示有效窗口与绝对时刻，不只写「窗口开始 + N 分钟」。
 * 时间片以 5 分钟为档；pct 只做点/条，不把满员当错误。
 * 读取失败禁用写入并给重试，不能让用户在未知当前值时覆盖。
 * 保存后说明实际执行可能受平台窗口影响；弹窗保持打开供核对读数。
 */

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Modal } from '@/components/ui/Modal'
import { notifySuccess } from '@/components/ui/toast'
import { ApiError } from '@/lib/api/client'
import { setTimePref } from '@/lib/api/schedule'
import type { TimePref } from '@/lib/api/schedule'
import { ME_KEYS, useTimePref } from './queries'

type Slot = NonNullable<TimePref['slots']>[number]

function prefErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '时间片不在有效窗口内。'
    if (error.status === 403) return '此部署未开放时间偏好。'
    if (error.status === 429) return '操作太频繁，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    return '保存失败，请稍后重试。'
  }
  return '网络异常，请稍后重试。'
}

function slotNote(slot: Slot): string {
  if (slot.pct >= 100) return '满员'
  if (slot.pct >= 80) return '紧张'
  return ''
}

function tickClass(slot: Slot, selected: boolean, allowed: boolean): string {
  const classes = ['uapp-ruler-tick']
  if (selected) classes.push('is-selected')
  if (slot.disabled || !allowed) classes.push('is-disabled')
  if (slot.pct >= 100) classes.push('is-full')
  else if (slot.pct >= 80) classes.push('is-tight')
  return classes.join(' ')
}

export function TimePrefDialog({
  open,
  onOpenChange,
  returnFocusTo,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  returnFocusTo?: HTMLElement | null
}) {
  const queryClient = useQueryClient()
  const prefQuery = useTimePref()
  const pref = prefQuery.data
  const stored = pref?.offset_min ?? null
  const [draft, setDraft] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [inlineNote, setInlineNote] = useState<string | null>(null)

  // 关合即清草稿：下次打开从已存值起选，不带上次未保存的选择。
  useEffect(() => {
    if (open) return
    setDraft(null)
    setError(null)
    setInlineNote(null)
  }, [open])

  // 尺比弹窗宽：打开且档位就绪后把已选档滚到视野中部，裁切边即横滑提示。
  const rulerRef = useRef<HTMLDivElement | null>(null)
  const didScrollRef = useRef(false)
  const selected = draft ?? stored
  const allowed = pref?.allowed ?? true
  const slots = pref?.slots ?? []
  const selectedSlot = slots.find((slot) => slot.offset_min === selected) ?? null
  const firstEnabledIndex = slots.findIndex((slot) => !slot.disabled && allowed)
  const selectedEnabledIndex = slots.findIndex(
    (slot) => slot.offset_min === selected && !slot.disabled && allowed,
  )
  const focusableIndex = selectedEnabledIndex >= 0 ? selectedEnabledIndex : firstEnabledIndex

  useEffect(() => {
    if (!open) {
      didScrollRef.current = false
      return
    }
    if (didScrollRef.current || slots.length === 0) return
    const tick = rulerRef.current?.querySelector<HTMLElement>('.uapp-ruler-tick.is-selected')
    tick?.scrollIntoView({ block: 'nearest', inline: 'center' })
    didScrollRef.current = true
  }, [open, slots.length])

  const saveMutation = useMutation({
    mutationFn: (offset: number | null) => setTimePref({ offset_min: offset }),
    onSuccess: (data, offset) => {
      queryClient.setQueryData(ME_KEYS.timePref, data)
      setDraft(null)
      setError(null)
      // toast 取服务端回读的档 label，不取本地草稿：读数与提示同源。
      const savedLabel = data.slots?.find((slot) => slot.offset_min === offset)?.label ?? ''
      notifySuccess(offset === null ? '已恢复自动分配' : `已保存 ${savedLabel}`)
    },
    onError: (err) => setError(prefErrorText(err)),
  })

  const canSave = allowed && draft !== null && draft !== stored && !saveMutation.isPending
  const canClear = allowed && stored !== null && !saveMutation.isPending

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="签到时间"
      description="在有效窗口内选一个执行偏好档。"
      returnFocusTo={returnFocusTo}
      wide
      scrollBody
      footer={
        <>
          <button
            type="button"
            className="uapp-button uapp-button--ghost"
            disabled={!canClear}
            onClick={() => {
              setError(null)
              saveMutation.mutate(null)
            }}
          >
            恢复自动
          </button>
          <button
            type="button"
            className="uapp-button uapp-button--primary"
            disabled={!canSave}
            onClick={() => {
              if (draft === null) return
              setError(null)
              saveMutation.mutate(draft)
            }}
          >
            {saveMutation.isPending ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <div className="uapp-set-dialog">
        {error ? (
          <div className="uapp-alert-block">
            <Alert tone="danger">{error}</Alert>
          </div>
        ) : null}
        {!allowed ? (
          <div className="uapp-alert-block">
            <Alert tone="warning">此部署未开放时间偏好；当前值只读。</Alert>
          </div>
        ) : null}

        {prefQuery.isPending ? (
          <div aria-busy="true">
            <div className="uapp-skeleton" style={{ width: 120, height: 34 }} />
            <div className="uapp-skeleton" style={{ width: '100%', height: 96, marginTop: 16, borderRadius: 'var(--radius-lg)' }} />
          </div>
        ) : prefQuery.isError ? (
          <div className="uapp-alert-block">
            <Alert tone="danger" title="读取失败">
              暂时读不到当前时间偏好；为避免覆盖未知值，保存已禁用。
            </Alert>
            <button
              type="button"
              className="uapp-button uapp-button--secondary"
              onClick={() => void prefQuery.refetch()}
            >
              重试
            </button>
          </div>
        ) : (
          <>
            <div className="uapp-set-dialog-readout">
              <div>
                <span className="uapp-row-sub">已选</span>
                <p className="uapp-hero-time uapp-hero-time--small">
                  {selectedSlot?.label ?? '自动分配'}
                </p>
              </div>
              <div className="uapp-set-dialog-readout-notes">
                <p className="uapp-hero-note">
                  {pref?.window
                    ? `有效窗口 ${pref.window.start}–${pref.window.end}`
                    : '有效窗口读取中'}
                </p>
                {pref?.estimated ? (
                  <p className="uapp-hero-note">
                    今日预计 {pref.estimated} · {pref.estimate_note}
                  </p>
                ) : null}
                {selectedSlot?.edge_note ? (
                  <p className="uapp-hero-note">{selectedSlot.edge_note}</p>
                ) : null}
              </div>
            </div>

            <div className="uapp-set-dialog-head">
              <h3 className="uapp-card-title">选择时间片</h3>
              <span className="uapp-row-sub">5 分钟一档 · 方向键或横滑选择</span>
            </div>

            {slots.length === 0 ? (
              <p className="uapp-card-sub">
                没有可用的时间片；账号审核中或今天没有启用的任务。
              </p>
            ) : (
              <>
                <div className="uapp-ruler" role="listbox" aria-label="签到时间片" ref={rulerRef}>
                  {slots.map((slot, index) => {
                    const isSelected = slot.offset_min === selected
                    const note = slotNote(slot)
                    const focusSlotAt = (targetIndex: number) => {
                      const candidate = slots[targetIndex]
                      if (!candidate || candidate.disabled || !allowed) return false
                      setDraft(candidate.offset_min)
                      setError(null)
                      setInlineNote(
                        candidate.reason ??
                          (slotNote(candidate) === '满员' ? '这一档已满，保存后可能顺延。' : null),
                      )
                      const options = rulerRef.current?.querySelectorAll<HTMLElement>('[role="option"]')
                      options?.[targetIndex]?.focus()
                      return true
                    }
                    return (
                      <button
                        key={slot.offset_min}
                        type="button"
                        role="option"
                        aria-selected={isSelected}
                        tabIndex={index === focusableIndex ? 0 : -1}
                        aria-label={`${slot.label}${slot.disabled ? `，${slot.reason ?? '不可选'}` : note ? `，${note}` : ''}`}
                        className={tickClass(slot, isSelected, allowed)}
                        disabled={slot.disabled || !allowed}
                        onClick={() => {
                          setDraft(slot.offset_min)
                          setError(null)
                          setInlineNote(slot.reason ?? (note === '满员' ? '这一档已满，保存后可能顺延。' : null))
                        }}
                        onKeyDown={(event) => {
                          let step = 0
                          if (event.key === 'ArrowRight') step = 1
                          else if (event.key === 'ArrowLeft') step = -1
                          else if (event.key === 'Home') {
                            event.preventDefault()
                            for (let i = 0; i < slots.length; i += 1) if (focusSlotAt(i)) break
                            return
                          } else if (event.key === 'End') {
                            event.preventDefault()
                            for (let i = slots.length - 1; i >= 0; i -= 1) if (focusSlotAt(i)) break
                            return
                          }
                          if (step === 0) return
                          event.preventDefault()
                          for (let i = index + step; i >= 0 && i < slots.length; i += step) {
                            if (focusSlotAt(i)) break
                          }
                        }}
                      >
                        <span className="uapp-ruler-bar" />
                        <span className="uapp-ruler-label">{slot.label}</span>
                        {note ? <span className="uapp-ruler-note">{note}</span> : null}
                      </button>
                    )
                  })}
                </div>
                {inlineNote ? <p className="uapp-card-sub">{inlineNote}</p> : null}
                <p className="uapp-card-sub">
                  实际执行可能受平台窗口影响；保存的是偏好，不是承诺时刻。
                </p>
              </>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}