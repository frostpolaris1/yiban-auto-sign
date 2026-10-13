/**
 * 巡检面契约（Vitest 单测）：`GET /api/admin/executor-health` 的消费侧（第二刀 ④）。
 *
 * 契约来源：`web/routes/executor_health_api.py` 与 `web/services/executor_env.py`
 * 的 `_executor_health_cards`。纪律：
 * · 身份（`identity` / `role` / `index` / `heartbeat_file`）四者同源，由服务端按
 *   **心跳文件名**解析；前端只渲染，不自拼身份、不读任何"记录体自称的 role"。
 * · 心跳过期/无记录都必须**显式**写出"无心跳"，不留白、不显示 0。
 * · 吞吐只按服务端下发的分子/分母渲染；前端不另算第二套口径。
 */

export type ExecState = "running" | "finished" | "idle" | "stale";
export type BadgeTone = "ok" | "bad" | "warn" | "info" | "muted";

export interface ExecHealthThroughput {
  /** 当日该执行体名下**了结**的账号数（分子） */
  done: number;
  claimed: number;
  failed: number;
  total: number;
  /** 有效签到窗口长度（秒，分母） */
  window_sec: number;
  /** 完成数 ÷ 窗口长度换算到每分钟；窗口为 0 时为 null（不编 0） */
  per_min: number | null;
}

export interface ExecHealthCard {
  /** 身份键：`worker-<槽位>` / `single` / `fallback`（与心跳文件名同源） */
  identity: string;
  identity_source: "heartbeat_file" | "config";
  heartbeat_file: string | null;
  role: string;
  index: number | null;
  /** 后端中文标签（角色名） */
  label: string;
  /** 用户设的行名；未设为 null */
  name: string | null;
  /** 是否在清单里（false = 清单外却在写心跳：配置说没有、机器上在跑） */
  configured: boolean;
  state: ExecState;
  /** 最近一次心跳时刻；无记录为 null */
  heartbeat_at: string | null;
  /** 心跳年龄（秒）；无记录为 null */
  age_sec: number | null;
  /** 是否活心跳：running 为 true、stale 为 false、idle/finished 为 null */
  fresh: boolean | null;
  last_seen_at: string | null;
  throughput: ExecHealthThroughput;
}

export interface ExecHealthPayload {
  ok: boolean;
  day: string;
  now: string;
  heartbeat_sec: number;
  freshness_limit_sec: number;
  window_sec: number;
  executors: ExecHealthCard[];
}

/** 执行体健康卡查询串（无查询参数）。 */
export function execHealthQuery(): string {
  return "/api/admin/executor-health";
}

/** 卡片的显示名：用户设的行名优先，否则用后端标签。 */
export function cardName(card: Pick<ExecHealthCard, "name" | "label">): string {
  return card.name || card.label;
}

/** 卡片的名册归属说明：清单外的心跳文件必须标出来（否则会被当成"已配置的执行体"）。 */
export function unconfiguredNote(card: Pick<ExecHealthCard, "configured">): string {
  return card.configured ? "" : "清单外";
}

/** 四态 → 徽标色调：运行=绿、可疑中断=红、未运行/已跑完=灰。 */
export function stateTone(state: ExecState): BadgeTone {
  if (state === "running") return "ok";
  if (state === "stale") return "bad";
  return "muted";
}

/** 四态中文标签（stale 是"需要注意"的那一态，措辞必须与"未运行"分得开）。 */
export function stateText(state: ExecState): string {
  switch (state) {
    case "running":
      return "运行中";
    case "stale":
      return "可疑中断";
    case "finished":
      return "已跑完";
    default:
      return "未运行";
  }
}

/** 心跳年龄文案：秒 / 分 / 时（负数按 0）。 */
export function ageText(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return "";
  const n = Math.max(0, Math.round(sec));
  if (n < 60) return `${n} 秒前`;
  if (n < 3600) return `${Math.floor(n / 60)} 分钟前`;
  return `${Math.floor(n / 3600)} 小时前`;
}

/** 时刻串里的时分秒（形状不符时原样回，空值回 "--:--:--"）。 */
export function clockText(ts: string | null | undefined): string {
  const text = String(ts ?? "");
  const m = text.match(/\d{2}:\d{2}:\d{2}/);
  return m ? m[0] : text || "--:--:--";
}

/**
 * 心跳一列的文案。**过期与无记录都必须出现"无心跳"字样**——留白或给 0 会让
 * "没有心跳"被读成"心跳正常"。
 */
export function heartbeatText(card: Pick<ExecHealthCard, "state" | "heartbeat_at" | "age_sec">): string {
  if (card.state === "stale") {
    if (!card.heartbeat_at) return "无心跳 · 已过期（时刻不可读）";
    return `无心跳 · 已过期（最后心跳 ${clockText(card.heartbeat_at)}，${ageText(card.age_sec)}）`;
  }
  if (card.state === "finished") {
    return card.heartbeat_at ? `已收尾（${clockText(card.heartbeat_at)}）` : "已跑完（无收尾时刻）";
  }
  if (!card.heartbeat_at) return "无心跳";
  return `心跳 ${clockText(card.heartbeat_at)}（${ageText(card.age_sec)}）`;
}

/** 吞吐文案：分子/分母/换算值一起给出，口径一眼可辨。 */
export function throughputText(card: Pick<ExecHealthCard, "throughput">): string {
  const tp = card.throughput;
  const winMin = tp.window_sec > 0 ? `${Math.round(tp.window_sec / 60)} 分` : "窗口不可用";
  const rate = tp.per_min === null || tp.per_min === undefined
    ? "--"
    : `${tp.per_min.toFixed(2)} 个/分`;
  return `完成 ${tp.done} 个 ÷ ${winMin} = ${rate}`;
}

/** 概览一行：各态各几个（空列表也要说得出"没有执行体"）。 */
export function healthSummary(executors: ExecHealthCard[]): string {
  if (!executors.length) return "没有执行体（清单为空，且状态目录里没有心跳文件）";
  const count = (s: ExecState): number => executors.filter((e) => e.state === s).length;
  const parts = [
    `运行 ${count("running")}`,
    `可疑中断 ${count("stale")}`,
    `未运行 ${count("idle")}`,
    `已跑完 ${count("finished")}`,
  ];
  return `共 ${executors.length} 个执行体：${parts.join(" · ")}`;
}
