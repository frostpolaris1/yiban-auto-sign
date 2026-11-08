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
 * 值全部为合成标识（school-a/b、合成学校甲/乙、登记手机号掩码、example.com 邮箱）。
 * 时间偏好卡对 user 才渲染，而展示会话固定 master ⇒ 不提供 TimePref 夹具。
 * 看板在展示机走 master 视角：窗口与容量各有夹具，两卡零请求。
 * 用户管理页的邮箱前缀固定 `master-one`／`admin-one`／`member-one`，明示合成口径。
 */

import type { Account, AccountPage, AccountStatus, AccountSummary } from './api/account'
import type { AuditStatusReport } from './api/audit'
import type { Photo, PhotoPage } from './api/photo'
import type { School, SchoolAdmin, SchoolPage } from './api/school'
import type { EngineSnapshot } from './api/engine'
import type { AdminTaskRow, TaskPage } from './api/task'
import type { UserPage } from './api/user'
import type {
  CapacityReport,
  SchedulePlan,
  ScheduleWindow,
  ScheduleWindowList,
} from './api/schedule'
import type { PrefStatsReport, SignStatsReport } from './api/stats'

let seq = 0

/** 容量报告要求来源是枚举，而窗口行的来源是 `string | null`；用这张表收窄。 */
const SCHEDULE_SOURCES: readonly CapacityReport['source'][] = [
  'platform',
  'cache',
  'fallback',
  'override',
]

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
  name = '',
): Account {
  seq += 1
  return {
    id,
    school_id: schoolId,
    owner_user_id: 'user-1',
    owner_email_masked: 'u***@example.com',
    name,
    display_name: name || `账号${phoneMasked.slice(-4)}`,
    phone_masked: phoneMasked,
    status,
    reject_reason: status === 'rejected' ? '合成驳回理由' : '',
    reviewed_at: status === 'pending' ? null : '2026-10-20T08:41:00+08:00',
    reviewed_by: status === 'pending' ? null : 'user-9',
    created_at: '2026-10-01T09:00:00+08:00',
    updated_at: '2026-10-20T16:41:00+08:00',
    deleted_at: null,
  }
}

/** 账号列表（覆盖 6 种状态）。 */
export function showcaseAccounts(): AccountPage {
  return {
    items: [
      account('acc-0001', '138****8000', 'pending', 'school-a', '东区宿舍'),
      account('acc-0002', '139****9000', 'active', 'school-a', '西区宿舍'),
      account('acc-0003', '135****0001', 'active', 'school-b', '图书馆'),
      account('acc-0004', '138****8000', 'paused_admin', 'school-b'),
      account('acc-0005', '139****9000', 'rejected', 'school-a', '待补照片'),
      account('acc-0006', '135****0001', 'credential_error', 'school-a'),
    ],
    next_cursor: '',
  }
}

/** 待处理账号计数。与账号列表夹具同源：只数未软删的 pending 与 rejected。 */
export function showcaseAccountSummary(): AccountSummary {
  const items = showcaseAccounts().items
  return {
    pending: items.filter((item) => item.status === 'pending').length,
    rejected: items.filter((item) => item.status === 'rejected').length,
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

/**
 * 图片库（展示构建）。
 *
 * 展示机无后端，图位由照片区画合成占位，不请求字节。
 * sha256 取单字符重复，ID 明示 demo 口径，不对应任何真实图片。
 */
export function showcasePhotos(): PhotoPage {
  const photo = (id: string, sha: string, width: number, height: number): Photo => ({
    id,
    sha256: sha.repeat(64),
    mime: 'image/png',
    width,
    height,
    bytes: 1024,
    created_at: '2026-10-20T09:00:00+08:00',
  })
  return {
    items: [
      photo('photo-demo-front', 'a', 640, 480),
      photo('photo-demo-campus', 'b', 800, 600),
      photo('photo-demo-gate', 'c', 480, 640),
    ],
    next_cursor: '',
  }
}

/**
 * 用户管理页（单页）。三条覆盖三个角色与两种状态，含空的上次登录。
 * 邮箱是保留域 `example.com`；前缀明示合成口径。
 */
export function showcaseUsers(): UserPage {
  return {
    items: [
      {
        id: 'user-a',
        email: 'master-one@example.com',
        role: 'master',
        status: 'active',
        must_change_password: false,
        account_count: 3,
        pending_count: 0,
        review_count: 0,
        created_at: '2026-10-01T09:00:00+08:00',
        last_login_at: '2026-10-28T09:12:00+08:00',
        deleted_at: null,
      },
      {
        id: 'user-b',
        email: 'admin-one@example.com',
        role: 'admin',
        status: 'active',
        must_change_password: true,
        account_count: 1,
        pending_count: 0,
        review_count: 0,
        created_at: '2026-10-02T09:00:00+08:00',
        last_login_at: null,
        deleted_at: null,
      },
      {
        id: 'user-c',
        email: 'member-one@example.com',
        role: 'user',
        status: 'disabled',
        must_change_password: false,
        account_count: 0,
        pending_count: 0,
        review_count: 0,
        created_at: '2026-10-03T09:00:00+08:00',
        last_login_at: '2026-10-20T18:30:00+08:00',
        deleted_at: null,
      },
      {
        id: 'user-d',
        email: 'member-two@example.com',
        role: 'user',
        status: 'active',
        must_change_password: false,
        account_count: 2,
        pending_count: 1,
        review_count: 1,
        created_at: '2026-10-04T09:00:00+08:00',
        last_login_at: null,
        deleted_at: null,
      },
    ],
    next_cursor: '',
    counts: { pending: 1, normal: 2, vacant: 1, deleted: 0 },
  }
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

/**
 * 看板容量体检的合成报告。窗口必须先在 `showcaseWindows` 里存在；
 * 找不到就抛错，让展示机暴露夹具缺口，不静默给零。
 * 任务 `task-night` 故意需求超容量，展示 danger 缺口态。
 */
export function showcaseCapacity(query: {
  day: string
  schoolId: string
  taskId: string
}): CapacityReport {
  const day = /^\d{4}-\d{2}-\d{2}$/.test(query.day) ? query.day : today()
  const row = showcaseWindows(day).items.find(
    (item) => item.school_id === query.schoolId && item.task_id === query.taskId,
  )
  const source = SCHEDULE_SOURCES.find((item) => item === row?.source) ?? null
  if (!row || row.start === null || row.end === null || source === null) {
    throw new Error(`showcaseCapacity: 没有可用的合成窗口 ${query.schoolId}:${query.taskId}`)
  }
  const numbers =
    row.task_id === 'task-night'
      ? {
          capacity: 20,
          demand: 26,
          shortfall: 6,
          binding: 'workers' as const,
          suggested_workers: 4,
        }
      : { capacity: 24, demand: 8, shortfall: 0, binding: 'workers' as const }
  return {
    school_id: row.school_id,
    task_id: row.task_id,
    day,
    source,
    window_start: row.start,
    window_end: row.end,
    ...numbers,
  }
}

/** 看板 30 天趋势与分布：固定合成日，不随展示机时钟漂移。
 * 末两天各带一个失败桶：今日终态 KPI 展示出非满分成功率与向上环比。 */
export function showcaseSignStats(): SignStatsReport {
  const days = Array.from({ length: 30 }, (_, index) => {
    const day = `2026-10-${String(index + 1).padStart(2, '0')}`
    return {
      day,
      pending: index === 29 ? 1 : 0,
      running: 0,
      success: index % 7 === 0 ? 2 : 3,
      failed: index >= 28 ? 1 : 0,
      skipped: index % 11 === 0 ? 1 : 0,
    }
  })
  return {
    days,
    total: 9,
    by_status: { pending: 1, running: 0, success: 8, failed: 4, skipped: 2 },
    days_missing: [],
  }
}

/** 看板自选拥挤度：按选中的合成窗口生成 16 个五分钟槽。 */
export function showcasePrefStats(query: {
  day: string
  schoolId: string
  taskId: string
}): PrefStatsReport {
  const day = /^\d{4}-\d{2}-\d{2}$/.test(query.day) ? query.day : today()
  const row = showcaseWindows(day).items.find(
    (item) => item.school_id === query.schoolId && item.task_id === query.taskId,
  )
  if (!row || row.start === null || row.end === null) {
    throw new Error(`showcasePrefStats: 没有可用的合成窗口 ${query.schoolId}:${query.taskId}`)
  }
  // 窗口时刻是 UTC ISO 串；展示机按旧线口径显示 +08:00 的钟点。
  const startDate = new Date(row.start)
  const hour = (startDate.getUTCHours() + 8) % 24
  const minute = startDate.getUTCMinutes()
  // 槽数与服务端同逻辑：整 5 分钟片，最多两小时。
  const spanMinutes = (Date.parse(row.end) - Date.parse(row.start)) / 60_000
  const slices = Math.max(0, Math.min(24, Math.floor(spanMinutes / 5)))
  const counts = [3, 5, 2, 1, 0, 0, 4, 6, 2, 1, 0, 3, 5, 2, 1, 0, 2, 3, 1, 0, 0, 1, 2, 0]
  const slots = Array.from({ length: slices }, (_, index) => {
    const total = hour * 60 + minute + index * 5
    return {
      offset_min: index * 5,
      label: `${String(Math.floor(total / 60) % 24).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`,
      count: counts[index] ?? 0,
      cap: 5,
    }
  })
  return {
    school_id: row.school_id,
    task_id: row.task_id,
    day,
    window_start: row.start,
    window_end: row.end,
    slots,
  }
}

/**
 * 看板审计链巡检的合成结论：完好态。
 * 链头是小写十六进制的重复串，明示合成；时刻固定，展示机不随日历漂移。
 */
export function showcaseAuditStatus(): AuditStatusReport {
  return {
    ok: true,
    empty: false,
    checked_rows: 128,
    last_hash: 'ab'.repeat(32),
    broken_at: null,
    checked_at: '2026-10-31T09:00:00+08:00',
  }
}

/**
 * 运行页（单页）。五条覆盖五个任务状态与两所学校，含未认领行。
 * 手机号只有掩码形态；学校与任务是合成词；时刻是合成业务日。
 */
export function showcaseRunPage(): TaskPage {
  const base = '2026-10-31'
  const at = (hour: number, minute: number) => `${base}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00+08:00`
  const rows: AdminTaskRow[] = [
    {
      id: 'task-0',
      account_id: 'account-1',
      account_phone_masked: '138****8001',
      school_id: 'school-a',
      school_name: '合成学校甲',
      task_name: '早签到',
      state: 'running',
      attempts: 1,
      result: '',
      run_at: at(8, 10),
      claimed_at: at(8, 10),
      updated_at: at(8, 12),
    },
    {
      id: 'task-1',
      account_id: 'account-2',
      account_phone_masked: '138****8002',
      school_id: 'school-a',
      school_name: '合成学校甲',
      task_name: '早签到',
      state: 'success',
      attempts: 1,
      result: 'ok',
      run_at: at(8, 20),
      claimed_at: at(8, 20),
      updated_at: at(8, 22),
    },
    {
      id: 'task-2',
      account_id: 'account-3',
      account_phone_masked: '138****8003',
      school_id: 'school-a',
      school_name: '合成学校甲',
      task_name: '早签到',
      state: 'failed',
      attempts: 3,
      result: 'credential_invalid',
      run_at: at(8, 30),
      claimed_at: at(8, 30),
      updated_at: at(8, 36),
    },
    {
      id: 'task-3',
      account_id: 'account-4',
      account_phone_masked: '138****8004',
      school_id: 'school-b',
      school_name: '合成学校乙',
      task_name: '晚自习签到',
      state: 'pending',
      attempts: 0,
      result: '',
      run_at: at(9, 0),
      claimed_at: null,
      updated_at: at(8, 40),
    },
    {
      id: 'task-4',
      account_id: 'account-5',
      account_phone_masked: '138****8005',
      school_id: 'school-b',
      school_name: '合成学校乙',
      task_name: '晚自习签到',
      state: 'skipped',
      attempts: 1,
      result: 'window_missed',
      run_at: at(9, 10),
      claimed_at: at(9, 10),
      updated_at: at(9, 11),
    },
  ]
  return { items: rows, next_cursor: '' }
}

/**
 * 执行监控的合成快照（UX-5）。
 *
 * 时刻相对展示机的当下取值：租约剩余与「最近一轮」才有活页面的样子。
 * 值仍是合成标识：掩码号取 138 保留段，学校名明示合成口径。
 */
export function showcaseEngineSnapshot(): EngineSnapshot {
  const at = (offsetMs: number): string => new Date(Date.now() + offsetMs).toISOString()
  const minute = 60_000
  return {
    inflight: [
      {
        account_id: 'account-1',
        account_phone_masked: '138****8001',
        school_id: 'school-a',
        school_name: '合成学校甲',
        claimed_at: at(-40_000),
        lease_expires_at: at(80_000),
      },
      {
        account_id: 'account-4',
        account_phone_masked: '138****8004',
        school_id: 'school-b',
        school_name: '合成学校乙',
        claimed_at: at(-15_000),
        lease_expires_at: at(105_000),
      },
    ],
    due_now: 3,
    today_success: 12,
    today_failed: 1,
    retrying: [
      {
        task_id: 'task-r1',
        account_id: 'account-3',
        account_phone_masked: '138****8003',
        school_id: 'school-a',
        school_name: '合成学校甲',
        task_name: '早签到',
        attempts: 2,
        run_at: at(5 * minute),
        result: 'transient',
      },
      {
        task_id: 'task-r2',
        account_id: 'account-3',
        account_phone_masked: '138****8003',
        school_id: 'school-a',
        school_name: '合成学校甲',
        task_name: '晚自习签到',
        attempts: 1,
        run_at: at(20 * minute),
        result: 'rejected',
      },
    ],
    retrying_truncated: false,
    last_claim_at: at(-40_000),
    last_tick_at: at(-2_000),
    worker_started_at: at(-6 * 60 * minute),
  }
}
