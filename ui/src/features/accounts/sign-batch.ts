/**
 * **功能**
 * 账号页「批量签到」的纯逻辑：逐项错误码的中文释义、失败说明与结果行构造。
 *
 * **归属**
 * `features/accounts` 域内纯函数：不引用组件、不取数、不发请求。
 *
 * **口径**
 * 契约 `POST /api/accounts/batch/sign` 不需要二次口令：整批只校验 1..10 与去重，
 * 通过后逐项独立入队，某项失败不阻断后续、也不回滚已入队项。
 * 因此失败原因必须逐项落到账号上，且必须是**中文**：原始 code 只作括号里的诊断线索，
 * 直接显示 `conflict` 之类的英文稳定码不算可读。
 * 引擎全局暂停时服务端回 `capacity_exceeded` 且 message 是英文 `engine is paused`，
 * 这里换成中文原因，不把英文稳定串端给用户。
 * 服务端给的 message 若本身是中文（可读），优先用它替代本表的分句。
 * 未登记的码保留原始 code 并给一句「未登记」说明，便于词表漂移可见。
 * 顺序与请求 ids 一致，映射不到账号的项回落到原始 id。
 */

/** 一次批量签到的上限，与契约 maxItems 一致。 */
export const BATCH_SIGN_LIMIT = 10

/** 全部入队成功时的 toast 文案。 */
export const SIGN_BATCH_TOAST = '批量签到已入队'

export type BatchSignGuide = { label: string; hint: string }

/** 逐项失败码的中文释义。键是契约 `ErrorCode` 的子集（本端点会出现的那些）。 */
const SIGN_CODE_GUIDES: Record<string, BatchSignGuide> = {
  not_found: { label: '账号不存在或无权访问', hint: '刷新列表后再试' },
  validation_failed: { label: '账号标识不合法', hint: '该行 ID 不是有效 UUID，跳过即可' },
  conflict: { label: '账号状态不允许签到', hint: '待审、暂停或凭据失效的账号需先恢复' },
  already_signed: { label: '今日已签到', hint: '该账号今天已经签到成功，无需重复入队' },
  sign_running: { label: '正在签到', hint: '上一次签到还在执行，稍后再试' },
  window_closed: { label: '签到窗口已关闭', hint: '当前不在可签到时段' },
  capacity_exceeded: { label: '执行容量已满', hint: '引擎并发已满，稍后再试' },
  rate_limited: { label: '请求过于频繁', hint: '该账号被限流，稍后再试' },
  forbidden: { label: '权限不足', hint: '批量签到只归主管理员与管理员' },
  unauthorized: { label: '登录已过期', hint: '重新登录后再试' },
  internal_error: { label: '服务内部错误', hint: '稍后重试；持续失败请查看运行台账' },
}

/** 未登记的码（含空值）走这一条：不猜含义，只说不认识。 */
const UNKNOWN_GUIDE: BatchSignGuide = {
  label: '未登记的失败原因',
  hint: '后端新增了错误码，前端词表尚未登记',
}

const HAS_CJK_RE = /[\u4e00-\u9fff]/

/** 引擎全局暂停的稳定英文串，只用于识别，不展示。 */
const ENGINE_PAUSED_MESSAGE = 'engine is paused'

/** 按 code 取中文释义；空值与未登记的码回落到统一说明。 */
export function describeBatchSignCode(code: string | undefined): BatchSignGuide {
  const key = (code ?? '').trim()
  return SIGN_CODE_GUIDES[key] ?? UNKNOWN_GUIDE
}

/**
 * 一项失败的可读说明：`中文原因（原始码）· 处置建议`。
 * 服务端 message 是中文且不与原因重复时，用它代替表里的建议。
 * 引擎暂停优先于码表：同一个 capacity_exceeded 两种含义必须分开说。
 */
export function describeBatchSignFailure(code?: string, message?: string): string {
  const guide = describeBatchSignCode(code)
  const raw = (message ?? '').trim()
  const paused = raw === ENGINE_PAUSED_MESSAGE
  const label = paused ? '执行引擎已暂停' : guide.label
  const hint = paused ? '全局暂停期间不接受手动签到，等引擎恢复后再试' : guide.hint
  const detail = raw !== '' && raw !== ENGINE_PAUSED_MESSAGE && HAS_CJK_RE.test(raw) ? raw : hint
  const base = code === undefined || code.trim() === '' ? label : `${label}（${code}）`
  return `${base} · ${detail}`
}

/** 结果区块的标题：先把「几条入队、几条失败」说清，再逐行展开。 */
export function signBatchSummary(result: {
  items: readonly { status: 'ok' | 'error' }[]
}): string {
  const ok = result.items.filter((item) => item.status === 'ok').length
  const failed = result.items.length - ok
  return `批量签到：${ok} 条已入队，${failed} 条失败`
}

export type BatchSignRow = {
  id: string
  /** 行标识：优先手机号掩码，映射不到时回落到原始 id。 */
  label: string
  ok: boolean
  /** 失败时是中文原因；成功时是入队说明。 */
  detail: string
}

/** 把契约响应摊成逐行结果，顺序与响应（即请求）一致。 */
export function batchSignRows(
  items: readonly { id: string; status: 'ok' | 'error'; code?: string; message?: string }[],
  labels: ReadonlyMap<string, string>,
): BatchSignRow[] {
  return items.map((item) => {
    const ok = item.status === 'ok'
    return {
      id: item.id,
      label: labels.get(item.id) ?? item.id,
      ok,
      detail: ok ? '已入队' : describeBatchSignFailure(item.code, item.message),
    }
  })
}
