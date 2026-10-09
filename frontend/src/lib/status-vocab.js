/* 签到状态词表 —— **前端显示层的单一事实源**（纯 JS 模块）。
 *
 * 为什么用纯 JS 而不是 TS：`tests/test_dashboard_stats_caliber_js.py` 会按字面量从
 * 本模块抽出 `STATUS_VOCAB` 对象、在 node 里真跑（同 dashboard/model.js 的约束）。
 * 故：
 *   · 表必须是标准对象字面量，且定义行写成「var 加表名加等号加左花括号」（抽取器靠该
 *     字面量 + 花括号配对定位）；
 *   · 上方的注释块内**不得**出现那一串定义字面量本身（抽取器先命中注释里的那次出现，
 *     就会从注释处数括号、永远配不平）；
 *   · 对象体内（含其中注释）不得出现不成对的 `{` / `}`——抽取器只数花括号；
 *   · 不要改成箭头函数 / 加 TS 注解。
 *
 * ## 为什么要有它
 * 收敛前，同一批状态码在三处各写一份中文，且**互相已经漂移**：
 *   · 仪表盘图表短名（`dashboard/model.js` STATUS_LABEL）；
 *   · 日志页事件徽标（`logs/format.ts` SIGN_STATUS_MAP）；
 *   · 账号表状态列（`accounts/model.js` STATE_TEXT/ICON/TONE）。
 * 例：`paused` 三处分别是「账密暂停 / 已暂停 / 账号暂停」；`skipped_window` 是
 * 「时段外跳过 / 超出时段 / 时段外跳过」。登记总账见
 * `tests/test_yiban_status_single_source.py`。
 *
 * ## 与服务端 DISPLAY 的关系（语境不同，非逐项对拍）
 * 本表是**前端三页展示**的单源；服务端 `yiban/status.py` 的 `DISPLAY` 是服务端语境的
 * 另一套（月历日期格 / 我的账号状态行经 `ctx.by_code` 下发），字段不保证与本表逐项相同。
 * 例如 `paused`：本表 tone 为 `bad`（账号表语义），服务端语境为 `warn`——这是两处消费
 * 语境的差异，不是缺陷，勿据此「对齐」而改掉任一侧。
 *
 * ## 记录形状
 * 每个状态码一条 `{ full, short, icon, tone }`：
 *   · `full`  —— 全称，供账号表状态列（信息量优先）；
 *   · `short` —— 短名，供图表/徽标等紧凑面（仪表盘图表、日志徽标）；
 *   · `icon`  —— lucide 图标名（账号表状态列；`#i-<name>` symbol）。
 *   · `tone`  —— **语义语气档**（ok/bad/warn/info/muted）。它只是语义；各页到 CSS
 *     类名的映射各自持有（账号页 `.acct-state--*` 无 info 档，见 accounts/model.js
 *     的 ACCT_TONE）。**不要**把它当成某个页面的类名直接用。
 *
 * ## 未知码兜底（信息不丢）
 * 本表只收已知状态码；表外未知码由各消费方按**各自**原语义兜底（口径本就不同，不强求划一）：
 *   · 仪表盘 / 日志：回落**原始码**可见（日志另加 muted，原始码仍进 `title`）；
 *   · 账号表：回落「待签」+ clock + muted——表外码在账号页不保证原始码可见。
 * 切勿把未知码静默并进某个已知档——那会让枚举膨胀后的成功率只抬不降。
 *
 * 键集合与 `yiban/status.py::ALL_STATUSES` 一致（由
 * `tests/test_yiban_status_single_source.py` 钉住，加状态码必须在此补一行）。
 */

export var STATUS_VOCAB = {
  success: { full: "签到成功", short: "成功", icon: "circle-check", tone: "ok" },
  already: { full: "已签到", short: "已签到", icon: "circle-check", tone: "ok" },
  no_task: { full: "无需签到", short: "无需签到", icon: "circle-minus", tone: "muted" },
  failed: { full: "签到失败", short: "失败", icon: "circle-x", tone: "bad" },
  retrying: { full: "重试中", short: "重试中", icon: "refresh-cw", tone: "warn" },
  skipped_window: { full: "时段外跳过", short: "时段外跳过", icon: "ban", tone: "warn" },
  skipped_norange: { full: "窗口缺失", short: "窗口缺失", icon: "ban", tone: "warn" },
  no_position: { full: "无点位", short: "无点位", icon: "ban", tone: "warn" },
  // 补签中：平台 signPosition 的 State=5（补签申请或流程进行中，结果未定）。
  // 语气档 warn 与服务端 DISPLAY 同档：不是完成，也不是"有意不签"。
  supplementing: { full: "补签中（结果未定）", short: "补签中", icon: "clock", tone: "warn" },
  paused: { full: "账号暂停", short: "账密暂停", icon: "circle-pause", tone: "bad" },
  user_cancelled: { full: "用户已取消", short: "用户取消", icon: "circle-stop", tone: "muted" },
  pending: { full: "待签", short: "待签", icon: "clock", tone: "info" },
  global_paused: { full: "全局暂停", short: "全局暂停", icon: "circle-pause", tone: "warn" },
};

/** 取某状态码的规范化词条；未知码返回 undefined（由消费方兜底）。 */
export function statusEntry(code) {
  return STATUS_VOCAB[code];
}

/** 未知码的默认图标 / 文案 / 语气档：三页兜底值本就不同，故由调用方各传各的。 */
export function statusField(code, field, fallback) {
  var e = STATUS_VOCAB[code];
  return e ? e[field] : fallback;
}
