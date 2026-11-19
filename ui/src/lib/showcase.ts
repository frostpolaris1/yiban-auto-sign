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
import type { NotifyPrefPage } from './api/adminNotify'
import type {
  AutoApproveStatus,
  QuotaReport,
  RegistrationStatus,
  ScheduleSettingsReport,
  SettingsReport,
} from './api/adminSettings'
import type { Announcement } from './api/announcement'
import type { AuditLogItem, AuditLogPage, AuditStatusReport } from './api/audit'
import type { HealthStatus } from './api/health'
import type { Photo, PhotoPage } from './api/photo'
import type { School, SchoolAdmin, SchoolPage } from './api/school'
import type { EngineSnapshot } from './api/engine'
import type { AccountDiagnostics, AdminTaskRow, TaskPage } from './api/task'
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
 * 看板审计链巡检的合成结论：缺省完好态；`broken` 供展示机切换断链样本。
 * 链头是小写十六进制的重复串，明示合成；时刻固定，展示机不随日历漂移。
 */
export function showcaseAuditStatus(broken = false): AuditStatusReport {
  if (broken) {
    return {
      ok: false,
      empty: false,
      checked_rows: 86,
      last_hash: 'cd'.repeat(32),
      broken_at: 87,
      checked_at: '2026-10-31T09:05:00+08:00',
      anchor_status: 'not_checked',
    }
  }
  return {
    ok: true,
    empty: false,
    checked_rows: 128,
    last_hash: 'ab'.repeat(32),
    broken_at: null,
    checked_at: '2026-10-31T09:00:00+08:00',
  }
}

/** 审计台账的合成行：固定时刻与合成标识，详情是脱敏后的 JSON 文本。 */
const SHOWCASE_AUDIT_ROWS: AuditLogItem[] = [
  {
    id: 1042,
    at: '2026-10-31T09:12:00+08:00',
    action: 'auth.login',
    actor: 'm***@example.com',
    target: 'user-1',
    detail: '{"ip":"eg-demo-1","result":"ok"}',
  },
  {
    id: 1041,
    at: '2026-10-31T09:08:00+08:00',
    action: 'account.review',
    actor: 'o***@example.com',
    target: 'acc-0002',
    detail: '{"decision":"approve","reason":""}',
  },
  {
    id: 1040,
    at: '2026-10-31T09:05:00+08:00',
    action: 'task.rearm',
    actor: 'o***@example.com',
    target: 'task-demo-1',
    detail: '{"attempts":2,"result":"transient"}',
  },
  {
    id: 1039,
    at: '2026-10-31T08:58:00+08:00',
    action: 'settings.engine_paused',
    actor: 'm***@example.com',
    target: 'engine',
    detail: '{"paused":true}',
  },
  {
    id: 1038,
    at: '2026-10-31T08:41:00+08:00',
    action: 'school.default_photo_set',
    actor: 'm***@example.com',
    target: 'school-a',
    detail: '{"photo_id":"photo-demo-gate"}',
  },
  {
    id: 1037,
    at: '2026-10-31T08:30:00+08:00',
    action: 'user.role_change',
    actor: 'm***@example.com',
    target: 'user-b',
    detail: '{"from":"user","to":"admin"}',
  },
  {
    id: 1036,
    at: '2026-10-31T08:12:00+08:00',
    action: 'account.credential_error',
    actor: 'system',
    target: 'acc-0003',
    detail:
      '{"code":"credential_invalid","note":"合成详情：用于验证长文换行与抽屉内滚动，不包含任何真实凭据或个人信息。"}',
  },
  {
    id: 1035,
    at: '2026-10-30T21:40:00+08:00',
    action: 'auth.logout',
    actor: 'a***@example.com',
    target: 'user-9',
    detail: '{}',
  },
]

/**
 * 审计台账（展示构建）：按契约参数在夹具内筛选与游标切片。
 * 展示机零网络；动作精确匹配，操作者忽略大小写，时间按 RFC3339 闭区间比较。
 */
export function showcaseAuditLogs(
  query: {
    action?: string
    actor?: string
    from?: string
    to?: string
    cursor?: string
    limit?: number
  } = {},
): AuditLogPage {
  const action = (query.action ?? '').trim()
  const actor = (query.actor ?? '').trim().toLowerCase()
  const from = query.from ? Date.parse(query.from) : null
  const to = query.to ? Date.parse(query.to) : null
  const filtered = SHOWCASE_AUDIT_ROWS.filter((row) => {
    if (action !== '' && row.action !== action) return false
    if (actor !== '' && row.actor.toLowerCase() !== actor) return false
    const at = Date.parse(row.at)
    if (from !== null && at < from) return false
    if (to !== null && at > to) return false
    return true
  })
  const limit = query.limit && query.limit > 0 ? query.limit : 20
  const cursorIndex = query.cursor?.startsWith('demo-audit-')
    ? Number(query.cursor.slice('demo-audit-'.length))
    : 0
  const start = Number.isFinite(cursorIndex) && cursorIndex > 0 ? cursorIndex : 0
  const items = filtered.slice(start, start + limit)
  const nextIndex = start + items.length
  const nextCursor = nextIndex < filtered.length ? `demo-audit-${nextIndex}` : ''
  return {
    items,
    next_cursor: nextCursor,
    total: filtered.length,
    has_more: nextCursor !== '',
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

/** 待办页（展示构建）：按状态筛选账号夹具，与侧栏待处理徽标同源。 */
export function showcaseQueueAccounts(
  status: 'pending' | 'rejected',
  filters: { schoolId?: string; q?: string } = {},
): AccountPage {
  const keyword = (filters.q ?? '').trim().toLowerCase()
  return {
    items: showcaseAccounts().items.filter((item) => {
      if (item.status !== status) return false
      if (filters.schoolId && item.school_id !== filters.schoolId) return false
      if (!keyword) return true
      return [item.display_name, item.name, item.phone_masked, item.owner_email_masked].some(
        (field) => field.toLowerCase().includes(keyword),
      )
    }),
    next_cursor: '',
  }
}

/** 待办页（展示构建）：按任务态与筛选条件筛当日任务夹具。 */
export function showcaseQueueTasks(
  state: 'failed' | 'pending' | 'running',
  filters: { schoolId?: string; q?: string } = {},
): TaskPage {
  const keyword = (filters.q ?? '').trim().toLowerCase()
  return {
    items: showcaseRunPage().items.filter((item) => {
      if (item.state !== state) return false
      if (filters.schoolId && item.school_id !== filters.schoolId) return false
      if (!keyword) return true
      return [item.task_name, item.school_name, item.account_phone_masked].some((field) =>
        field.toLowerCase().includes(keyword),
      )
    }),
    next_cursor: '',
  }
}

/** 待办页诊断抽屉（展示构建）：按对象构造合成诊断快照。 */
export function showcaseQueueDiagnostics(accountId: string): AccountDiagnostics {
  const acc = showcaseAccounts().items.find((item) => item.id === accountId)
  const task = showcaseRunPage().items.find((item) => item.account_id === accountId)
  const day = today()
  return {
    account_id: accountId,
    display_name: acc?.display_name ?? `账号${(task?.account_phone_masked ?? '0001').slice(-4)}`,
    phone_masked: acc?.phone_masked ?? task?.account_phone_masked ?? '138****8001',
    account_status: acc?.status ?? 'active',
    state: task?.state ?? null,
    run_at: task?.run_at ?? null,
    attempts: task?.attempts ?? 0,
    result: task?.result ?? '',
    next_retry_at: task?.state === 'failed' ? iso(day, 9 * 60 + 40) : null,
    suggestion: task?.state === 'failed' ? 'retry_now' : 'wait',
    window: {
      start: iso(day, 8 * 60 + 30),
      end: iso(day, 9 * 60),
      source: 'cache',
      version: 2,
    },
    egress: { identity: 'https://eg-demo-1' },
  }
}

/** 账号设置页（展示构建）：合成管理员通知订阅 4×3 默认矩阵，零网络请求。 */
export function showcaseAdminNotify(): NotifyPrefPage {
  return {
    items: [
      { category: 'credential_invalid', channel: 'in_app', enabled: true },
      { category: 'credential_invalid', channel: 'email', enabled: false },
      { category: 'credential_invalid', channel: 'webhook', enabled: true },
      { category: 'review_pending', channel: 'in_app', enabled: true },
      { category: 'review_pending', channel: 'email', enabled: true },
      { category: 'review_pending', channel: 'webhook', enabled: true },
      { category: 'sign_failed_daily', channel: 'in_app', enabled: false },
      { category: 'sign_failed_daily', channel: 'email', enabled: true },
      { category: 'sign_failed_daily', channel: 'webhook', enabled: false },
      { category: 'capacity_rejected', channel: 'in_app', enabled: true },
      { category: 'capacity_rejected', channel: 'email', enabled: false },
      { category: 'capacity_rejected', channel: 'webhook', enabled: false },
    ],
  }
}

/** 系统页（展示构建）：注册开关合成夹具。 */
export function showcaseRegistrationStatus(): RegistrationStatus {
  return {
    enabled: false,
    terms_version: 1,
    privacy_version: 1,
  }
}

/** 系统页（展示构建）：新号自动审核开关合成夹具。 */
export function showcaseAutoApprove(): AutoApproveStatus {
  return { enabled: true }
}

/** 系统页（展示构建）：全局配额合成夹具（synthetic 演示数值，不连后端）。 */
export function showcaseQuotaSettings(): QuotaReport {
  return {
    max_users: { value: 20, source: 'db' },
    max_accounts: { value: 20, source: 'db' },
  }
}

/** 系统页（展示构建）：全部 8 项运行调度策略合成夹具。 */
export function showcaseScheduleSettings(): ScheduleSettingsReport {
  return {
    edge_front_sec: { value: 60, source: 'default' },
    edge_back_sec: { value: 60, source: 'default' },
    allow_time_pref: { value: true, source: 'db' },
    sunday_sign: { value: false, source: 'default' },
    saturday_sign: { value: true, source: 'db' },
    default_sign_start_min: { value: 390, source: 'default' },
    default_sign_end_min: { value: 470, source: 'default' },
    sign_dist: { value: 'front', source: 'default' },
  }
}

/** 系统页（展示构建）：全站公告合成夹具。 */
export function showcaseAnnouncement(): Announcement {
  return {
    text: '示例公告：系统每晚自动更新次日签到计划，请在每日 06:00 前确认账号状态。',
    updated_at: '2026-11-18T10:00:00Z',
  }
}

/**
 * 系统页（展示构建）：配置名册合成夹具（仅含合成示例键，敏感键脱敏）。
 * 三态来源与敏感两态齐备：env/db/default/unknown × 已设置（固定掩码）/未设置（空掩码）。
 */
export function showcaseAdminSettings(): SettingsReport {
  return {
    groups: [
      {
        name: '调度',
        items: [
          {
            key: 'YIBAN_WINDOW_EDGE_FRONT_SEC',
            group: '调度',
            source: 'default',
            value: 60,
            sensitive: null,
          },
          {
            key: 'YIBAN_WINDOW_EDGE_BACK_SEC',
            group: '调度',
            source: 'default',
            value: 60,
            sensitive: null,
          },
          {
            key: 'YIBAN_ALLOW_TIME_PREF',
            group: '调度',
            source: 'db',
            value: true,
            sensitive: null,
          },
        ],
      },
      {
        name: '发布',
        items: [
          {
            key: 'YIBAN_RESET_MAIL_ENABLE',
            group: '发布',
            source: 'default',
            value: false,
            sensitive: null,
          },
        ],
      },
      {
        name: '通知',
        items: [
          {
            key: 'YIBAN_MAIL_PASS',
            group: '通知',
            source: 'env',
            value: null,
            sensitive: { set: true, masked: '****', source: 'env' },
          },
          {
            key: 'YIBAN_WEBHOOK_SECRET',
            group: '通知',
            source: 'unknown',
            value: null,
            sensitive: { set: false, masked: '', source: 'unknown' },
          },
          {
            key: 'YIBAN_WEBHOOK_ENABLE',
            group: '通知',
            source: 'default',
            value: false,
            sensitive: null,
          },
        ],
      },
      {
        name: '部署',
        items: [
          {
            key: 'YIBAN_HTTP_ADDR',
            group: '部署',
            source: 'env',
            value: ':8080',
            sensitive: null,
          },
          {
            key: 'YIBAN_DATABASE_URL',
            group: '部署',
            source: 'env',
            value: null,
            sensitive: { set: true, masked: '****', source: 'env' },
          },
          {
            key: 'YIBAN_AUDIT_ANCHOR_FILE',
            group: '部署',
            source: 'default',
            value: '',
            sensitive: null,
          },
        ],
      },
    ],
  }
}

/** 系统页（展示构建）：存活自检合成夹具。 */
export function showcaseHealthStatus(): HealthStatus {
  return { status: 'ok' }
}
