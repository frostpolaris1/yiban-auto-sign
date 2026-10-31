/**
 * **功能**
 * 共用件画廊：UX-0 三件（AsyncState、BusyButton、FilterField）的演示。
 *
 * **归属**
 * `features/design` 域组件：只组合通用件。
 *
 * **口径**
 * 三个演示都是真实交互：状态切换、提交在途、空关键字校验。
 * 状态只在演示组件内存里，不落库、不进 URL。
 * 文案是固定演示词，不是占位乱码。
 */

import { useState } from 'react'
import { AsyncState, type AsyncStatus } from '@/components/ui/AsyncState'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { FilterField } from '@/components/ui/FilterField'

const ASYNC_DEMO_STATES: Array<{ value: AsyncStatus; label: string }> = [
  { value: 'loading', label: '加载中' },
  { value: 'error', label: '错误' },
  { value: 'empty', label: '空' },
  { value: 'data', label: '数据' },
]

/** 在途时长：给观察者留出看见禁用态的时间。 */
const BUSY_MS = 1200

export function SharedSection() {
  const [asyncStatus, setAsyncStatus] = useState<AsyncStatus>('loading')

  const [busy, setBusy] = useState(false)
  const [saves, setSaves] = useState(0)

  const [keyword, setKeyword] = useState('')
  const [keywordError, setKeywordError] = useState<string | undefined>(undefined)

  function save() {
    if (busy) return
    setSaves((count) => count + 1)
    setBusy(true)
    window.setTimeout(() => setBusy(false), BUSY_MS)
  }

  function submitFilter() {
    setKeywordError(keyword.trim() === '' ? '关键字不能为空' : undefined)
  }

  return (
    <Card className="col-6" eyebrow="Shared" title="UX-0 共用件">
      <div className="app-demo-stack">
        <div>
          <div className="demo-row">
            {ASYNC_DEMO_STATES.map((state) => (
              <Button
                key={state.value}
                size="sm"
                variant={asyncStatus === state.value ? 'primary' : 'ghost'}
                onClick={() => setAsyncStatus(state.value)}
              >
                {state.label}
              </Button>
            ))}
          </div>
          <AsyncState
            status={asyncStatus}
            loadingLabel="正在读取运行日志…"
            emptyLabel="今日暂无运行日志"
            error="读取运行日志失败，请检查网络后重试"
            onRetry={() => setAsyncStatus('data')}
          >
            <p style={{ margin: 0 }}>运行日志 3 条：08:00 / 12:30 / 18:00。</p>
          </AsyncState>
        </div>

        <div className="demo-row">
          <BusyButton variant="primary" busy={busy} busyLabel="保存中…" onClick={save}>
            保存设置
          </BusyButton>
          <span className="app-muted">已保存 {saves} 次</span>
        </div>

        <div style={{ maxWidth: 320 }}>
          <FilterField
            id="demo-filter-keyword"
            label="关键字"
            hint="按学校名或编号筛选"
            error={keywordError}
          >
            <input
              className="input"
              value={keyword}
              placeholder="示例：合成学校甲"
              onChange={(event) => {
                setKeyword(event.target.value)
                setKeywordError(undefined)
              }}
            />
          </FilterField>
          <div className="demo-row" style={{ marginTop: 12 }}>
            <Button variant="secondary" onClick={submitFilter}>
              提交筛选
            </Button>
          </div>
        </div>
      </div>
    </Card>
  )
}
