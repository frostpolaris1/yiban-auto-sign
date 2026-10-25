/**
 * **功能**
 * 今日签到状态卡：四行账号与状态标签。
 *
 * **归属**
 * `features/dashboard` 域组件。
 *
 * **口径**
 * 行数据是演示值。标签色只表达视觉分类。
 * 状态语义以引擎定论为准。本页不定义状态判据。
 */

import { Link } from '@tanstack/react-router'
import { Icon } from '@/components/ui/Icon'
import { Tag, type TagTone } from '@/components/ui/Badge'

type StatusRow = {
  name: string
  school: string
  status: string
  tone: TagTone
  time: string
}

const ROWS: StatusRow[] = [
  { name: 'stu-1042', school: '北校区', status: '已签到', tone: 't-active', time: '06:41' },
  { name: 'stu-1043', school: '北校区', status: '等待中', tone: 't-old', time: '06:52' },
  { name: 'stu-2088', school: '南校区', status: '凭据错误', tone: 't-unavail', time: '—' },
  { name: 'stu-2090', school: '南校区', status: '已签到', tone: 't-active', time: '07:03' },
]

/**
 * 表格标记照 Adminator 的 `src/index.html` 与 `src/basic-table.html`。
 * 结构为 `.table` 套 `.table-scroll`，状态用 `.tag` 药丸。
 */
export function SignStatusCard() {
  return (
    <section className="col-6 card">
      <div className="card-head">
        <div className="card-title-wrap">
          <span className="eyebrow">Today</span>
          <h2 className="card-title">今日签到状态</h2>
        </div>
        <Link className="card-action" to="/design">
          查看组件
          <Icon name="arrowRight" />
        </Link>
      </div>

      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">账号</th>
              <th scope="col">学校</th>
              <th scope="col">状态</th>
              <th scope="col" style={{ textAlign: 'right' }}>
                时刻
              </th>
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => (
              <tr key={row.name}>
                <td className="cell-name">{row.name}</td>
                <td className="cell-date">{row.school}</td>
                <td>
                  <Tag tone={row.tone}>{row.status}</Tag>
                </td>
                <td className="cell-date" style={{ textAlign: 'right' }}>
                  {row.time}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
