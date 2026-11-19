/**
 * **功能**
 * 顶栏全局搜索：`.cmd` 触发按钮 + 轻量结果面板。
 *
 * **归属**
 * `features/search`：由 `app` 装配层经 AppShell 的 `renderSearch` 插槽注入顶栏。
 * 壳层（components/shell）不反向导入本目录，方向门咬这条线。
 *
 * **口径**
 * Ctrl/Cmd+K 打开并聚焦输入；↑↓ 在结果间移动活动项；Enter 打开活动项；
 * Esc 关闭并把焦点交还触发按钮。键盘模型自实现，参考 `Menu` 的
 * 「焦点自己管、高亮一个点」思路——但搜索要管的是结果列表而非菜单项，
 * 故用 combobox/listbox 语义自己接键。不引新依赖（任务书明令）。
 * 输入防抖 250ms；旧请求以序号作废，迟到的响应不得覆盖新结果；
 * 清空输入或关闭面板同样作废在途请求。
 * 结果按角色收敛（见 searchResults）；无结果给提示词与示例。
 * 点击跳转：账号→`/accounts`（q+locate 高亮），用户→`/users?q=`，
 * 任务→`/run?q=`（本基点台账在 /run；`/logs` 仍是重定向桩）。
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from '@tanstack/react-router'
import { Icon } from '@/components/ui/Icon'
import type { SessionUser } from '@/lib/api/auth'
import { groupsFor, search, searchKindLabel } from './searchResults'
import type { SearchKind, SearchResult } from './searchResults'

const DEBOUNCE_MS = 250

const GROUP_ICONS: Record<SearchKind, 'accounts' | 'userCog' | 'listTodo'> = {
  account: 'accounts',
  user: 'userCog',
  task: 'listTodo',
}

export function GlobalSearch({ me }: { me: SessionUser }) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [partial, setPartial] = useState(false)
  const [loading, setLoading] = useState(false)
  const [active, setActive] = useState(-1)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const seqRef = useRef(0)

  const close = useCallback(() => {
    seqRef.current += 1
    setOpen(false)
    setTerm('')
    setResults([])
    setPartial(false)
    setLoading(false)
    setActive(-1)
  }, [])

  // Ctrl/Cmd+K：开着就聚焦输入，关着就开面板。浏览器默认行为一律吃掉。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setOpen((current) => !current)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // 面板打开后聚焦输入；关闭路径不在此处（Esc 自己负责焦点回还）。
  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  // 点击面板与触发按钮之外即关闭。
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (panelRef.current?.contains(target)) return
      if (triggerRef.current?.contains(target)) return
      close()
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open, close])

  // 防抖 250ms 后取数；序号作废迟到的旧响应。
  useEffect(() => {
    if (!open) return
    const trimmed = term.trim()
    if (trimmed === '') {
      seqRef.current += 1
      setResults([])
      setPartial(false)
      setLoading(false)
      setActive(-1)
      return
    }
    const timer = setTimeout(() => {
      seqRef.current += 1
      const seq = seqRef.current
      setLoading(true)
      void search(term, me.role)
        .then((outcome) => {
          if (seqRef.current !== seq) return
          setResults(outcome.results)
          setPartial(outcome.partial)
          setLoading(false)
          setActive(outcome.results.length > 0 ? 0 : -1)
        })
        .catch(() => {
          // search 内部已兜底；这层只是不让 Promise 悬挂。
          if (seqRef.current !== seq) return
          setPartial(true)
          setLoading(false)
        })
    }, DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [term, open, me.role])

  // 分组视图：组顺序按角色门，组内保持端点返回序。
  const groups = useMemo(() => {
    const map = new Map<SearchKind, SearchResult[]>()
    for (const row of results) {
      const bucket = map.get(row.kind) ?? []
      bucket.push(row)
      map.set(row.kind, bucket)
    }
    return groupsFor(me.role)
      .filter((kind) => map.has(kind))
      .map((kind) => ({ kind, rows: map.get(kind) as SearchResult[] }))
  }, [results, me.role])

  const openResult = (row: SearchResult) => {
    close()
    if (row.route === 'accounts') {
      void navigate({
        to: '/accounts',
        search: { q: row.query || undefined, locate: row.locateId },
      })
    } else if (row.route === 'users') {
      void navigate({ to: '/users', search: { q: row.query || undefined } })
    } else {
      // 当 02/04 链合入后此路由此处改为 /logs?tab=events&q=
      void navigate({ to: '/run', search: { q: row.query || undefined } })
    }
  }

  const onInputKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (results.length === 0) return
      const count = results.length
      if (event.key === 'ArrowDown') {
        setActive((current) => (current >= count - 1 ? 0 : current + 1))
      } else {
        setActive((current) => (current <= 0 ? count - 1 : current - 1))
      }
    } else if (event.key === 'Enter') {
      event.preventDefault()
      const row = results[active]
      if (row) openResult(row)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      close()
      triggerRef.current?.focus()
    }
  }

  const listId = 'global-search-listbox'

  return (
    <>
      <button
        type="button"
        className="cmd"
        ref={triggerRef}
        aria-label="全局搜索"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? 'global-search-panel' : undefined}
        title="全局搜索（Ctrl+K）"
        onClick={() => setOpen((current) => !current)}
      >
        <Icon name="search" />
        <span>搜索…</span>
        <kbd className="kbd">Ctrl K</kbd>
      </button>

      {open
        ? createPortal(
            <div
              className="app-search-panel"
              id="global-search-panel"
              ref={panelRef}
              role="dialog"
              aria-label="全局搜索"
            >
              <input
                ref={inputRef}
                type="text"
                className="app-search-input"
                role="combobox"
                aria-expanded={open}
                aria-controls={listId}
                aria-activedescendant={active >= 0 ? `search-option-${active}` : undefined}
                aria-label="搜索账号、用户或任务"
                placeholder="搜手机号掩码、备注名、邮箱或任务名…"
                value={term}
                onChange={(event) => setTerm(event.target.value)}
                onKeyDown={onInputKeyDown}
              />
              <div id={listId} role="listbox" aria-label="搜索结果">
                {term.trim() === '' ? (
                  <p className="app-muted">输入即搜：试手机号掩码（138****8000）、备注名或邮箱。</p>
                ) : null}
                {groups.map(({ kind, rows }, groupIndex) => (
                  <div key={kind} role="group" aria-label={searchKindLabel(kind)}>
                    <div className="app-search-group-label">
                      <Icon name={GROUP_ICONS[kind]} size={12} />
                      {searchKindLabel(kind)}
                    </div>
                    {rows.map((row, rowIndex) => {
                      const flat =
                        groups.slice(0, groupIndex).reduce((sum, g) => sum + g.rows.length, 0) +
                        rowIndex
                      return (
                        <div
                          key={`${row.kind}-${row.id}`}
                          id={`search-option-${flat}`}
                          role="option"
                          aria-selected={flat === active}
                          className="app-search-option"
                          onMouseEnter={() => setActive(flat)}
                          onClick={() => openResult(row)}
                        >
                          <span className="app-search-option-title">{row.title}</span>
                          <span className="app-search-option-sub">{row.subtitle}</span>
                        </div>
                      )
                    })}
                  </div>
                ))}
                {loading && term.trim() !== '' ? <p className="app-muted">搜索中…</p> : null}
                {!loading && term.trim() !== '' && results.length === 0 ? (
                  groupsFor(me.role).length === 0 ? (
                    <p className="app-muted">当前角色不启用全局检索。</p>
                  ) : (
                    <p className="app-muted">
                      没有匹配结果。试试掩码手机号（138****8000）或归属邮箱（o***@example.com）。
                    </p>
                  )
                ) : null}
                {partial ? <p className="app-caption">部分结果未能加载，可稍后重试。</p> : null}
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  )
}
