/**
 * **功能**
 * 实时连接横幅：断线给重连、补发给提示、结束给原因与重连。
 *
 * **归属**
 * `features/run` 的展示件：运行页与执行监控页共用这一份文案与按钮。
 *
 * **口径**
 * 四种状态互斥，同一时刻只渲染一块横幅；`live` 不渲染。
 * 补发只是中间态，不给重连按钮；结束态必须给重连出口，
 * 否则用户只能刷新页面。文案不在两个视图里各写一份。
 */

import { Button } from '@/components/ui/Button'
import type { RunLive } from './useRunLive'

export function RunLiveBanner({ live }: { live: RunLive }) {
  if (live.status === 'disconnected') {
    return (
      <div className="run-live-banner" role="status">
        <span>已停止实时，最近更新 {live.since ?? '—'}。</span>
        <Button variant="outline-primary" size="sm" onClick={live.reconnect}>
          重连
        </Button>
      </div>
    )
  }
  if (live.status === 'resyncing') {
    return (
      <div className="run-live-banner" role="status">
        <span>正在补发事件…</span>
      </div>
    )
  }
  if (live.status === 'ended') {
    return (
      <div className="run-live-banner" role="status">
        <span>实时流已结束：{live.endedReason ?? '服务器关闭连接'}</span>
        <Button variant="outline-primary" size="sm" onClick={live.reconnect}>
          重连
        </Button>
      </div>
    )
  }
  return null
}
