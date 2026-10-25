import { useState } from 'react'
import { Card } from '@/components/ui/Card'
import { SwitchControl } from '@/components/ui/SwitchControl'
import { Tabs } from '@/components/ui/Tabs'

const PILLS = ['日', '周', '月', '年'] as const

/** Tabs and switches. DOM follows Adminator `src/ui.html` and `src/forms.html`. */
export function NavigationSection() {
  const [pill, setPill] = useState<(typeof PILLS)[number]>('周')

  return (
    <Card className="col-6" eyebrow="Navigation" title="标签页与开关">
      <div className="app-demo-stack">
        <Tabs
          label="签到面板"
          items={[
            {
              value: 'overview',
              label: '概览',
              content: <p className="app-muted">概览面板：签到结果与失败原因汇总。</p>,
            },
            {
              value: 'activity',
              label: '活动',
              content: <p className="app-muted">活动面板：每条尝试的时间与结局。</p>,
            },
            {
              value: 'settings',
              label: '设置',
              content: <p className="app-muted">设置面板：时间片与通知偏好。</p>,
            },
          ]}
        />

        <div>
          <div className="tabs pills" role="tablist" aria-label="时间粒度">
            {PILLS.map((item) => (
              <button
                key={item}
                type="button"
                role="tab"
                aria-selected={pill === item}
                className={pill === item ? 'tab is-active' : 'tab'}
                onClick={() => setPill(item)}
              >
                {item}
              </button>
            ))}
          </div>
          <p className="app-caption">药丸变体：`.tabs.pills`。</p>
        </div>

        <div className="app-demo-stack" style={{ gap: 12 }}>
          <SwitchControl label="启用双因子认证" />
          <SwitchControl label="窗口开始前提醒" defaultChecked />
          <SwitchControl label="自动保存草稿" defaultChecked />
          <SwitchControl label="Beta 功能（锁定）" disabled />
        </div>
      </div>
    </Card>
  )
}
