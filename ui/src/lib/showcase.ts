/**
 * **功能**
 * 展示模式（VITE_SHOWCASE=1）的合成数据源：各页查询的替代取数函数。
 *
 * **归属**
 * `lib` 工具层：仅供展示构建消费；正常构建下 tree-shake 掉。
 *
 * **口径**
 * 形状与契约响应一致（手写合成对象，字段取 schema.d.ts 类型）。
 * 展示机无后端：各页查询以这些函数为 queryFn，页面完整可看、零网络请求。
 * 值全部为合成标识（school-a/b、合成学校甲/乙、登记手机号掩码）。
 * 时间偏好卡对 user 才渲染，而展示会话固定 master ⇒ 不提供 TimePref 夹具。
 */

import type { Account, AccountPage, AccountStatus } from './api/account'
import type { School, SchoolAdmin, SchoolPage } from './api/school'
import type {
  SchedulePlan,
  ScheduleWindow,
  ScheduleWindowList,
} from './api/schedule'

let seq = 0
function iso(base: string, addMinutes: number): string {
  const d = new Date(`${base}T00:00:00+08:00`)
  d.setMinutes(d.getMinutes() + addMinutes)
  return d.toISOString()
}

function account(
  id: string,
  phoneMasked: string,
  status: AccountStatus,
  schoolId: string,
): Account {
  seq += 1
  return {
    id,
    school_id: schoolId,
    owner_user_id: 'user-1',
    phone_masked: phoneMasked,
    status,
    reject_reason: status === 'rejected' ? '合成驳回理由' : '',
    reviewed_at: status === 'pending' ? null : '2026-10-20T08:41:00+08:00',
    reviewed_by: status === 'pending' ? null : 'user-9',
    created_at: '2026-10-01T09:00:00+08:00',
    updated_at: '2026-10-20T16:41:00+08:00',
  }
}

/** 账号列表（覆盖 6 种状态）。 */
export function showcaseAccounts(): AccountPage {
  return {
    items: [
      account('acc-0001', '138****8000', 'pending', 'school-a'),
      account('acc-0002', '139****9000', 'active', 'school-a'),
      account('acc-0003', '135****0001', 'active', 'school-b'),
      account('acc-0004', '138****8000', 'paused_admin', 'school-b'),
      account('acc-0005', '139****9000', 'rejected', 'school-a'),
      account('acc-0006', '135****0001', 'credential_error', 'school-a'),
    ],
    next_cursor: '',
  }
}

const SHOWCASE_SCHOOLS: School[] = [
  {
    id: 'school-a',
    code: 'demo-a',
    name: '合成学校甲',
    status: 'active',
    timezone: 'Asia/Shanghai',
    created_at: '2026-10-01T09:00:00+08:00',
    updated_at: '2026-10-20T16:41:00+08:00',
  },
  {
    id: 'school-b',
    code: 'demo-b',
    name: '合成学校乙',
    status: 'active',
    timezone: 'Asia/Shanghai',
    created_at: '2026-10-02T09:00:00+08:00',
    updated_at: '2026-10-21T16:41:00+08:00',
  },
]

/** 学校全量列表（账号页筛选用，对应 listAllSchools 的裸数组形状）。 */
export function showcaseAllSchools(): School[] {
  return SHOWCASE_SCHOOLS
}

/** 学校列表页（单页）。 */
export function showcaseSchools(): SchoolPage {
  return { items: SHOWCASE_SCHOOLS, next_cursor: '' }
}

/** 学校管理员全量（对应 listAllSchoolAdmins 的裸数组形状）。 */
export function showcaseSchoolAdmins(): SchoolAdmin[] {
  const admins: SchoolAdmin[] = [
    {
      user_id: 'user-9',
      email: 'admin-one@example.com',
      role: 'admin',
      granted_by: 'user-1',
      created_at: '2026-10-05T10:00:00+08:00',
    },
    {
      user_id: 'user-8',
      email: 'admin-two@example.com',
      role: 'master',
      granted_by: 'user-1',
      created_at: '2026-10-06T10:00:00+08:00',
    },
  ]
  return admins
}

function window(
  schoolId: string,
  schoolName: string,
  taskId: string,
  taskName: string,
  day: string,
  startMin: number | null,
  endMin: number | null,
  source: ScheduleWindow['source'],
): ScheduleWindow {
  return {
    school_id: schoolId,
    school_name: schoolName,
    task_id: taskId,
    task_name: taskName,
    enabled: true,
    day,
    start: startMin === null ? null : iso(day, startMin),
    end: endMin === null ? null : iso(day, endMin),
    source,
    version: 3,
  }
}

function today(): string {
  return new Date().toISOString().slice(0, 10)
}

/** 窗口一览（两校三任务，覆盖四种来源与一个无窗口行）。 */
export function showcaseWindows(day?: string): ScheduleWindowList {
  const resolvedDay = day && /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : today()
  return {
    day: resolvedDay,
    items: [
      window('school-a', '合成学校甲', 'task-daily', '每日点名', resolvedDay, 8 * 60 + 30, 9 * 60, 'override'),
      window('school-a', '合成学校甲', 'task-weekly', '每周晨检', resolvedDay, 12 * 60, 12 * 60 + 30, 'cache'),
      window('school-b', '合成学校乙', 'task-night', '晚间点名', resolvedDay, 20 * 60, 21 * 60, 'platform'),
      window('school-b', '合成学校乙', 'task-legacy', '旧版签到', resolvedDay, null, null, null),
    ],
  }
}

function plan(day: string, startMin: number, endMin: number, source: SchedulePlan['source']): SchedulePlan {
  const rows = Array.from({ length: 8 }, (_, i) => ({
    account_id: `acc-000${i + 1}`,
    run_at: iso(day, startMin + i * 4),
  }))
  const histogram: Record<string, number> = {}
  for (let i = 0; i < 8; i += 1) {
    const key = String(Math.floor(i / 2))
    histogram[key] = (histogram[key] ?? 0) + 1
  }
  return {
    day,
    enabled: true,
    source,
    window_start: iso(day, startMin),
    window_end: iso(day, endMin),
    version: source === 'override' ? 3 : 0,
    capacity: 24,
    demand: 8,
    shortfall: 0,
    histogram,
    rows,
  }
}

/** 计划预览／落库／覆盖共用的合成计划。 */
export function showcasePlan(day: string): SchedulePlan {
  return plan(day && /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : today(), 8 * 60 + 30, 9 * 60, 'override')
}
