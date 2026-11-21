/**
 * **功能**
 * 审计筛选状态：动作、操作者、起止日期与已提交条件。
 *
 * **归属**
 * `features/audit` 状态钩子：只管一页筛选，不发请求。
 *
 * **口径**
 * 动作与操作者输入带 250ms 防抖，回车立即提交；任何提交都通知调用方清空游标。
 * 日期是成对草稿：改动只更新草稿，停止输入 500ms 后校验整对并一次性提交，
 * 避免「先填起点发一次查询、再填终点又报错」的中间态；区间倒置保留两边的草稿值。
 * 错误只标记出错字段：起止倒置时标最后编辑的那一侧，起点合法就不标起点。
 * `hasActive` 只反映已生效条件（草稿中未提交不算），用于空态文案与清除按钮。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { parseDay } from '@/lib/format'

export const AUDIT_FILTER_DEBOUNCE_MS = 250
export const AUDIT_DATE_DEBOUNCE_MS = 500
export const AUDIT_DATE_RANGE_ERROR = '起始日期不能晚于结束日期'
export const AUDIT_DATE_FORMAT_ERROR = '日期格式无效，请使用 YYYY-MM-DD'

type Applied = { action: string; actor: string }

export type AuditDateError = {
  field: 'from' | 'to'
  message: string
}

export type AuditFiltersControls = {
  action: string
  actor: string
  from: string
  to: string
  hasActive: boolean
  actionInput: string
  actorInput: string
  fromDraft: string
  toDraft: string
  error: AuditDateError | null
  setActionInput: (value: string) => void
  setActorInput: (value: string) => void
  setFrom: (value: string) => void
  setTo: (value: string) => void
  commitNow: () => void
  clear: () => void
}

/** 校验一对日期草稿；`edited` 决定区间倒置时错误落在哪一侧。 */
function validateRange(
  fromDraft: string,
  toDraft: string,
  edited: 'from' | 'to',
): AuditDateError | null {
  if (fromDraft !== '' && parseDay(fromDraft) === null) {
    return { field: 'from', message: AUDIT_DATE_FORMAT_ERROR }
  }
  if (toDraft !== '' && parseDay(toDraft) === null) {
    return { field: 'to', message: AUDIT_DATE_FORMAT_ERROR }
  }
  if (fromDraft !== '' && toDraft !== '' && fromDraft > toDraft) {
    return { field: edited, message: AUDIT_DATE_RANGE_ERROR }
  }
  return null
}

export function useAuditFilters(onChange: () => void): AuditFiltersControls {
  const [actionInput, setActionInput] = useState('')
  const [actorInput, setActorInput] = useState('')
  const [applied, setApplied] = useState<Applied>({ action: '', actor: '' })
  const [from, setFromState] = useState('')
  const [to, setToState] = useState('')
  const [fromDraft, setFromDraft] = useState('')
  const [toDraft, setToDraft] = useState('')
  const [error, setError] = useState<AuditDateError | null>(null)
  const lastDateFieldRef = useRef<'from' | 'to'>('to')

  const changeRef = useRef(onChange)
  useEffect(() => {
    changeRef.current = onChange
  }, [onChange])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const trimmed = actionInput.trim()
      if (trimmed === applied.action) return
      setApplied((prev) => ({ ...prev, action: trimmed }))
      changeRef.current()
    }, AUDIT_FILTER_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [actionInput, applied.action])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const trimmed = actorInput.trim()
      if (trimmed === applied.actor) return
      setApplied((prev) => ({ ...prev, actor: trimmed }))
      changeRef.current()
    }, AUDIT_FILTER_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [actorInput, applied.actor])

  const commitDates = useCallback(
    (nextFrom: string, nextTo: string, edited: 'from' | 'to') => {
      const dateError = validateRange(nextFrom, nextTo, edited)
      if (dateError) {
        setError(dateError)
        return
      }
      setError(null)
      if (nextFrom === from && nextTo === to) return
      setFromState(nextFrom)
      setToState(nextTo)
      changeRef.current()
    },
    [from, to],
  )

  // 日期成对提交：草稿稳定 500ms 后校验整对，只发一次查询。
  useEffect(() => {
    if (fromDraft === from && toDraft === to) {
      setError((previous) => (previous === null ? previous : null))
      return
    }
    const timer = window.setTimeout(() => {
      commitDates(fromDraft, toDraft, lastDateFieldRef.current)
    }, AUDIT_DATE_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [fromDraft, toDraft, from, to, commitDates])

  const setFrom = useCallback((value: string) => {
    lastDateFieldRef.current = 'from'
    setFromDraft(value)
  }, [])

  const setTo = useCallback((value: string) => {
    lastDateFieldRef.current = 'to'
    setToDraft(value)
  }, [])

  /**
   * 回车立即提交：日期先过整对校验，再与文本条件一起落到已生效状态。
   * 日期有错时整次提交都不生效，避免界面同时出现错误与新查询。
   */
  const commitNow = useCallback(() => {
    const datesChanged = fromDraft !== from || toDraft !== to
    if (datesChanged) {
      const dateError = validateRange(fromDraft, toDraft, lastDateFieldRef.current)
      if (dateError) {
        setError(dateError)
        return
      }
      setError(null)
    }

    const nextAction = actionInput.trim()
    const nextActor = actorInput.trim()
    const actionChanged = nextAction !== applied.action
    const actorChanged = nextActor !== applied.actor
    if (!datesChanged && !actionChanged && !actorChanged) return

    if (actionChanged || actorChanged) {
      setApplied({
        action: actionChanged ? nextAction : applied.action,
        actor: actorChanged ? nextActor : applied.actor,
      })
    }
    if (datesChanged) {
      setFromState(fromDraft)
      setToState(toDraft)
    }
    changeRef.current()
  }, [
    actionInput,
    actorInput,
    applied.action,
    applied.actor,
    fromDraft,
    toDraft,
    from,
    to,
  ])

  const clear = useCallback(() => {
    setActionInput('')
    setActorInput('')
    setApplied({ action: '', actor: '' })
    setFromState('')
    setToState('')
    setFromDraft('')
    setToDraft('')
    setError(null)
    changeRef.current()
  }, [])

  return {
    action: applied.action,
    actor: applied.actor,
    from,
    to,
    hasActive: Boolean(applied.action || applied.actor || from || to),
    actionInput,
    actorInput,
    fromDraft,
    toDraft,
    error,
    setActionInput,
    setActorInput,
    setFrom,
    setTo,
    commitNow,
    clear,
  }
}
