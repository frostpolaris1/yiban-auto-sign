/**
 * **功能**
 * 组内筛选状态：学校、搜索输入与已提交关键字。输入带 250ms 防抖，
 * 回车立即提交；任何提交通知调用方重置本组游标。
 *
 * **归属**
 * `features/queue` 状态钩子：只管一组筛选，不发请求。
 *
 * **口径**
 * 三组各自持有本钩子实例，学校与关键字互不串扰；
 * `hasActive` 只反映已生效条件（输入中未提交不算），用于空态文案与清除按钮。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export const SEARCH_DEBOUNCE_MS = 250

export type QueueFilters = {
  schoolId: string
  input: string
  q: string
  hasActive: boolean
  setSchoolId: (schoolId: string) => void
  setInput: (value: string) => void
  commitNow: () => void
  clear: () => void
}

export function useQueueFilters(onChange: () => void): QueueFilters {
  const [schoolId, setSchoolIdState] = useState('')
  const [input, setInput] = useState('')
  const [q, setQ] = useState('')
  const changeRef = useRef(onChange)
  const qRef = useRef(q)
  const inputRef = useRef(input)

  useEffect(() => {
    changeRef.current = onChange
  }, [onChange])

  useEffect(() => {
    qRef.current = q
    inputRef.current = input
  }, [q, input])

  const setSchoolId = useCallback((next: string) => {
    setSchoolIdState(next)
    changeRef.current()
  }, [])

  const commitNow = useCallback(() => {
    const trimmed = inputRef.current.trim()
    if (trimmed === qRef.current) return
    setQ(trimmed)
    changeRef.current()
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const trimmed = input.trim()
      if (trimmed === qRef.current) return
      setQ(trimmed)
      changeRef.current()
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [input])

  const clear = useCallback(() => {
    setSchoolIdState('')
    setInput('')
    setQ('')
    changeRef.current()
  }, [])

  return {
    schoolId,
    input,
    q,
    hasActive: Boolean(schoolId || q),
    setSchoolId,
    setInput,
    commitNow,
    clear,
  }
}
