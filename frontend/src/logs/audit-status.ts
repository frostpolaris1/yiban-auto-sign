/**
 * 巡检面契约（Vitest 单测）：`GET /api/admin/audit-status` 的消费侧（第二刀 ⑤）。
 *
 * 契约来源：`web/routes/audit_api.py::api_audit_status` 与
 * `yiban/store/audit_chain.py` 的 `audit_verdict` / `audit_known_debt`。纪律：
 * · 三态互斥且只取三值：健康 / 检出篡改 / **无法定论**；"判不出来"绝不当成健康，
 *   也不静默略过（页面必须把它显示出来）。
 * · 已知欠账单独一行、用**中性色调**，不与"篡改"同色。
 * · 只渲染服务端下发的计数与时刻，不出账号明细；缓存结果必须带上数据时刻。
 */

export type AuditState = "healthy" | "tampered" | "indeterminate";
export type BadgeTone = "ok" | "bad" | "warn" | "info" | "muted";

export interface AuditKnownDebt {
  code: string;
  label: string;
  count: number;
  reason: string;
}

export interface AuditAnchor {
  /** 锚点文件的可读状态：ok / missing / io-error / decode-error */
  path_state: string;
  lines: number;
  /** 2026-08-28 前写入的老格式（三字段）行数 */
  old_format_lines: number;
  unparsable_lines: number;
  /** 当前定点基准（max_id 最大的真行）；无可定点时为 null */
  max_id: number | null;
  max_ts: string | null;
}

export interface AuditStatusPayload {
  ok: boolean;
  state: AuditState;
  /** 与 scripts/audit_verify.py 的退出码同源：0 健康 / 1 篡改 / 2 无法定论 */
  verdict_code: 0 | 1 | 2;
  reason: string;
  cached: boolean;
  data_at: string | null;
  chain_ok: boolean | null;
  broken: number | null;
  anchor_status: string | null;
  anchor: AuditAnchor | null;
  indeterminate: { present: boolean; reason: string };
  known_debt: AuditKnownDebt[];
  write_failures: number | null;
  empty_hash_rows: number | null;
  note: string;
}

/** 审计链状态块查询串（无查询参数）。 */
export function auditStatusQuery(): string {
  return "/api/admin/audit-status";
}

/** 三态 → 徽标色调：健康=绿、检出篡改=红、无法定论=**黄**（既不绿也不红）。 */
export function auditTone(state: AuditState | string): BadgeTone {
  if (state === "healthy") return "ok";
  if (state === "tampered") return "bad";
  return "warn";
}

/** 三态中文标签。未知状态一律按"无法定论"显示（不猜、不当健康）。 */
export function auditStateText(state: AuditState | string): string {
  if (state === "healthy") return "健康";
  if (state === "tampered") return "检出篡改";
  return "无法定论";
}

/** "校验到哪个锚点"一行。锚点缺失/无可定点都如实写明，不留白。 */
export function anchorText(p: Pick<AuditStatusPayload, "anchor">): string {
  const a = p.anchor;
  if (!a) return "校验到锚点：无（本次没有可读的锚点信息）";
  if (a.path_state !== "ok") {
    return `校验到锚点：读不出（${a.path_state}，共登记 ${a.lines} 行）`;
  }
  if (a.max_id === null) {
    return `校验到锚点：没有可作定点基准的行（共 ${a.lines} 行）`;
  }
  return `校验到锚点：${a.max_ts || "时刻未知"}（第 ${a.max_id} 行，共 ${a.lines} 行）`;
}

/** 数据时刻一行：缓存结果必须自报"这份结论是什么时候跑出来的"。 */
export function dataAtText(p: Pick<AuditStatusPayload, "cached" | "data_at">): string {
  if (!p.cached || !p.data_at) return "数据时刻：无（尚无体检结果）";
  return `数据时刻：${p.data_at}（每日自检结果）`;
}

/** "判不出来的区间"一行：没有则不显示（回空串）。 */
export function indeterminateText(
  p: Pick<AuditStatusPayload, "indeterminate" | "state">,
): string {
  if (!p.indeterminate?.present) return "";
  const reason = p.indeterminate.reason || p.reason || "";
  return `判不出来的区间：${reason}`;
}

/**
 * 已知欠账逐行文案（**与三态分开显示**：它不是篡改、也不是无法定论）。
 * 无欠账回空数组（页面据此不渲染这一块）。
 */
export function knownDebtLines(p: Pick<AuditStatusPayload, "known_debt">): string[] {
  const debt = Array.isArray(p.known_debt) ? p.known_debt : [];
  return debt.map((d) => {
    const head = `已知欠账：${d.label} ${d.count} 行`;
    return d.reason ? `${head} —— ${d.reason}` : head;
  });
}

/** 明细补充行（诊断文本 + 欠账计数），没有则回空串。 */
export function auditDetailText(
  p: Pick<AuditStatusPayload, "chain_ok" | "broken" | "write_failures" | "empty_hash_rows" | "note">,
): string {
  const parts: string[] = [];
  if (p.chain_ok === false) parts.push(`链内自洽：失败（断链 ${p.broken ?? "?"} 处）`);
  if (p.empty_hash_rows) parts.push(`空 hash 行 ${p.empty_hash_rows} 条`);
  if (p.write_failures) parts.push(`审计写入欠账 ${p.write_failures} 次`);
  if (p.note) parts.push(p.note);
  return parts.join("；");
}
