/**
 * **功能**
 * `/system#roster` 系统名册分区：全量配置键的只读分组视图。
 *
 * **归属**
 * `features/system/roster` 分区组件：仅由 `SystemView` 装配。
 *
 * **口径**
 * - 数据源恰一次 `GET /api/admin/settings`（master/admin）；与调度策略写回共享
 *   `['admin-settings']` 缓存键，写回后缓存即时更新，不再补发请求。
 * - 组→键→值/来源三态如实：非敏感键显解析值与来源；
 *   敏感键只显「已设置/未设置＋固定掩码＋来源」，视图层不消费 `value` 字段，
 *   畸形出包（敏感键带原值）也不渲染原值。
 * - 只读分区：数据态零编辑控件、零写请求；展示构建走合成夹具。
 */

import { useQuery } from '@tanstack/react-query'
import { AsyncState } from '@/components/ui/AsyncState'
import { Badge } from '@/components/ui/Badge'
import { Card } from '@/components/ui/Card'
import {
  ADMIN_SETTINGS_QUERY_KEY,
  getAdminSettings,
  type SettingItem,
  type SettingsReport,
} from '@/lib/api/adminSettings'
import { showcaseAdminSettings } from '@/lib/showcase'
import { SHOWCASE } from '@/lib/showcase-mode'
import {
  formatRosterReadError,
  formatSettingValue,
  rosterSourceLabel,
  rosterSourceTone,
  sensitiveSettingView,
} from './drafts'

function RosterItemRow({ item }: { item: SettingItem }) {
  const isSensitive = item.sensitive !== null
  const view = isSensitive ? sensitiveSettingView(item) : null
  const source = view ? view.source : item.source
  return (
    <li className="sys-roster-item">
      <code className="sys-roster-key">{item.key}</code>
      <span className="sys-roster-value">
        {view ? (
          <>
            <span>{view.stateLabel}</span>
            {view.masked ? <span className="sys-roster-mask">{view.masked}</span> : null}
          </>
        ) : (
          <span>{formatSettingValue(item.value)}</span>
        )}
      </span>
      <Badge tone={rosterSourceTone(source)}>{rosterSourceLabel(source)}</Badge>
    </li>
  )
}

function RosterGroups({ report }: { report: SettingsReport }) {
  return (
    <div className="sys-roster-groups">
      {report.groups.map((group) => (
        <section key={group.name} className="sys-roster-group" aria-label={`名册分组 ${group.name}`}>
          <h3 className="sys-roster-group-title">
            {group.name}
            <span className="sys-roster-group-count">{group.items.length} 键</span>
          </h3>
          <ul className="sys-roster-items">
            {group.items.map((item) => (
              <RosterItemRow key={item.key} item={item} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}

export function RosterSection() {
  const query = useQuery({
    queryKey: ADMIN_SETTINGS_QUERY_KEY,
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseAdminSettings()) : getAdminSettings()),
    staleTime: 30_000,
  })

  const groups = query.data?.groups ?? []
  const asyncStatus = query.isPending
    ? 'loading'
    : query.isError
      ? 'error'
      : groups.length === 0
        ? 'empty'
        : 'data'

  return (
    <section id="roster" className="sys-section" tabIndex={-1} aria-labelledby="roster-section-heading">
      <div className="sys-section-head">
        <h2 id="roster-section-heading" className="sys-section-title">
          系统名册
        </h2>
        <p className="sys-section-desc">
          全站配置键的只读名册：分组、取值与来源如实展示；敏感键永不显示原值，只给「已设置 / 掩码 / 来源」。
        </p>
      </div>

      <Card eyebrow="Settings Roster" title="配置名册（只读）" className="sys-policy-card sys-policy-card-wide">
        <AsyncState
          status={asyncStatus}
          error={query.isError ? formatRosterReadError(query.error) : undefined}
          onRetry={() => void query.refetch()}
          emptyLabel="名册为空：服务端未注册任何配置键。"
        >
          {query.data && groups.length > 0 ? <RosterGroups report={query.data} /> : null}
        </AsyncState>
      </Card>
    </section>
  )
}
