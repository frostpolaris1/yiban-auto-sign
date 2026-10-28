/**
 * **功能**
 * 看板表格的账号格：序号主行加短 id 副行。
 *
 * **归属**
 * `features/dashboard` 展示层：只排版，不取数。
 *
 * **口径**
 * 签到状态端点与诊断端点都只给 `account_id`，没有掩码手机号。
 * 主行给人读（`账号 #N`），副行给对日志用（短 id）。
 * 两张卡共用这一格，避免两处格式漂移。
 */

type AccountLabelProps = {
  accountId: string
  /** 列表内序号，从 0 起。 */
  index: number
}

/** 短 id：超过 12 字符时截断，保留前缀便于对日志。 */
export function shortId(id: string): string {
  return id.length <= 12 ? id : `${id.slice(0, 8)}…`
}

export function AccountLabel({ accountId, index }: AccountLabelProps) {
  return (
    <>
      <div>{`账号 #${index + 1}`}</div>
      <div className="app-caption data-cell-mono">{shortId(accountId)}</div>
    </>
  )
}
