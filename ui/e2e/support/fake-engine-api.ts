import type { EngineSnapshot } from '../../src/lib/api/engine'

/**
 * 运行巡检页签的快照夹具（UX-5）。
 *
 * 两份快照只用登记过的合成值：掩码手机号取 138 保留段，学校名明示合成口径。
 * `ENGINE_SNAPSHOT` 时刻相对当下取值，租约剩余与倒计时才有可断言的活值；
 * `ENGINE_VISUAL_SNAPSHOT` 派生自固定页钟 `VISUAL_NOW`，视觉基线跨日不漂。
 * 路由与调用记录由 `fake-logs-api` 组合登记；SSE 经 `installFakeEventSource` 注入。
 */

/** 相对当下的 ISO 时刻。 */
function at(offsetMs: number): string {
  return new Date(Date.now() + offsetMs).toISOString()
}

const MINUTE = 60_000

/**
 * 每次取用时按**调用时刻**重算的活快照。
 *
 * 行为用例共用 `ENGINE_SNAPSHOT` 有个隐性缺陷：它是模块级常量，时刻在 import
 * 那一刻就定死了。整份行为套件单进程串行（`workers: 1`），巡检用例排在队列
 * 130+ 位、真正开跑常在两三分钟以后，80 秒的租约早已到期——表格整列「已到期」，
 * 断言「1 分」稳定失败（CI run 44894613811 / 44896708760 都红在这条）。
 * 夹具要的是「此刻还剩 1 分多钟」，故改成按需生成。
 */
export function liveEngineSnapshot(): EngineSnapshot {
  return {
    ...ENGINE_SNAPSHOT,
    inflight: ENGINE_SNAPSHOT.inflight.map((row, index) => ({
      ...row,
      claimed_at: at(index === 0 ? -40_000 : -15_000),
      lease_expires_at: at(index === 0 ? 80_000 : 105_000),
    })),
    last_claim_at: at(-40_000),
    last_tick_at: at(-2_000),
  }
}

/** 登记过的合成快照：两个在飞、两个待重试（同账号两条）、计数非零。 */
export const ENGINE_SNAPSHOT: EngineSnapshot = {
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
      run_at: at(5 * MINUTE),
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
      run_at: at(20 * MINUTE),
      result: 'rejected',
    },
  ],
  retrying_truncated: false,
  last_claim_at: at(-40_000),
  last_tick_at: at(-2_000),
  worker_started_at: at(-6 * 60 * MINUTE),
}

/**
 * 视觉基线的页钟时刻：固定值，不随真实墙钟。
 * `/engine` 的视觉用例在 goto 前 `page.clock.setFixedTime(new Date(VISUAL_NOW))`，
 * 页内 `Date.now()` 与本夹具同源——租约剩余恒为「1 时 30 分」，
 * 相对时刻恒落在 formatClock 分支，基线跨日、跨分钟都不漂。
 */
export const VISUAL_NOW = Date.parse('2030-01-01T09:00:00+08:00')

/** 以 VISUAL_NOW 为基准的固定 ISO 时刻。 */
function visualAt(offsetMs: number): string {
  return new Date(VISUAL_NOW + offsetMs).toISOString()
}

/**
 * 视觉基线的合成快照：全部时刻派生自 VISUAL_NOW，零 Date.now()。
 * 值仍是合成标识：掩码号取 138 保留段，学校名明示合成口径。
 */
export const ENGINE_VISUAL_SNAPSHOT: EngineSnapshot = {
  ...ENGINE_SNAPSHOT,
  inflight: [
    {
      account_id: 'account-1',
      account_phone_masked: '138****8001',
      school_id: 'school-a',
      school_name: '合成学校甲',
      claimed_at: visualAt(-30 * MINUTE),
      lease_expires_at: visualAt(90 * MINUTE),
    },
    {
      account_id: 'account-4',
      account_phone_masked: '138****8004',
      school_id: 'school-b',
      school_name: '合成学校乙',
      claimed_at: visualAt(-25 * MINUTE),
      lease_expires_at: visualAt(95 * MINUTE),
    },
  ],
  retrying: [
    {
      task_id: 'task-r1',
      account_id: 'account-3',
      account_phone_masked: '138****8003',
      school_id: 'school-a',
      school_name: '合成学校甲',
      task_name: '早签到',
      attempts: 2,
      run_at: visualAt(60 * MINUTE),
      result: 'transient',
    },
  ],
  last_claim_at: visualAt(-2 * 60 * MINUTE),
  last_tick_at: visualAt(-2 * 60 * MINUTE),
  worker_started_at: visualAt(-6 * 60 * MINUTE),
}

export type FakeEngineApi = {
  calls: { method: string; path: string }[]
}

/** 执行监控快照的调用记录。 */
export function engineCalls(api: FakeEngineApi) {
  return api.calls.filter((call) => call.path === '/api/admin/engine' && call.method === 'GET')
}
