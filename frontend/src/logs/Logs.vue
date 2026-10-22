<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { api, pollMs, shellBase, toast } from "../lib/shell";
import { usePolling } from "../lib/polling";
import { isValidDate } from "./date-guard.js";
import {
  ALL_LOG_LEVEL,
  WARN_LOG_LEVEL,
  EVENT_PAGE_SIZES,
  LOG_PAGE_SIZE,
  TAB_KEYS,
  buildExportUrl,
  buildLogsQuery,
  eventCountText,
  eventMessage,
  pagedInfoText,
  paginate,
  shouldPoll,
  sortEvents,
  statusLabel,
  type LogEvent,
  type LogsPayload,
  type TabKey,
} from "./format";
import {
  buildRunEventsQuery,
  emptyRoundsText,
  eventTime,
  findRound,
  formatDuration,
  nextSelectedKey,
  needsExecutorFallback,
  roundKey,
  roundsTruncatedText,
  windowText,
  type RunEvent,
  type RunEventsPayload,
  type RunRound,
} from "./run-events";
import {
  applyProgress,
  emptyStreamState,
  execRowNote,
  isTerminalEnd,
  recentText,
  stateFromSnapshot,
  streamStatusText,
  windowLine,
  type StreamProgress,
  type StreamSnapshot,
  type StreamState,
} from "./stream";
import {
  cardName,
  execHealthQuery,
  healthSummary,
  heartbeatText,
  stateText as execStateText,
  stateTone as execStateTone,
  throughputText,
  unconfiguredNote,
  type ExecHealthCard,
  type ExecHealthPayload,
} from "./exec-health";
import {
  anchorText,
  auditDetailText,
  auditStateText,
  auditStatusQuery,
  auditTone,
  dataAtText,
  indeterminateText,
  knownDebtLines,
  type AuditStatusPayload,
} from "./audit-status";

/* 签到日志页（GET /api/logs + GET /api/admin/run-events，四分区内容：运行巡检 /
   日志 / 签到事件 / 探针记录）。

   纪律：
   · 脱敏单出口在服务端（日志行 _mask_log_phones、事件与进度流 phone _mask_phone、
     执行体只回角色与槽位），前端只有插值渲染、**零自遮**；全程零 v-html。
   · 三元组 total_lines/returned/truncated 由服务端同轴推出，前端只按 truncated 显示
     「已截断」，不得用行数自行推断（见 format.ts）。
   · 级别档：默认档位只有服务端一份（前端不内联默认值）。首屏不带 level，
     档位显示以服务端回执为准；用户切换后才显式下发。收起过行时信息栏与空态都
     显式写出「已收起 N 行 INFO／DEBUG」——不许让人把收起读成没有那些行。
   · 运行进度只保留 14 天（服务端 `run_events.RETENTION_DAYS`）。页面显式写明窗口；
     窗口外给「跨月回溯请走审计日志页」的指引，不静默出空表。
   · 日志正文走**服务端分页**（每页 200 行，只渲染当前页）；事件表的翻页/排序
     仍是客户端行为（接口按日返回当日事件集，封顶 5000 行）。

   日期语义（2026-10-03 修 blocking：原实现用服务端回显日期覆盖用户选择，导致
   查看该日/回到今天/空态跳转全部失效）：
   · `viewDate === ""` = **跟随最新**（服务端解析最近有日志的一天，通常是今天）；
   · 「查看该日」「空态跳转」pin 住某天并写进 `?date=`（可分享、刷新不丢）；pin 态**不轮询**；
   · 「回到今天」清空 = 回到跟随态。
   运行巡检块跟随同一个日期，故两块内容始终同一天。 */

const AUTO_KEY = "yiban-logs-autorefresh";

interface TableState {
  page: number;
  size: number;
  prop: "time" | "phone" | "status" | "attempt" | "message";
  order: "ascending" | "descending";
}

const tab = ref<TabKey>("main");
const payload = ref<LogsPayload | null>(null);
const loading = ref(false);
const errorText = ref("");
const dateInput = ref("");
/** 用户 pin 的日期；"" = 跟随最新（与 legacy state.viewDate 同口径） */
const viewDate = ref("");
/** 服务端解析出的实际日期（导出与轮询刷新用） */
const loadedDate = ref("");
const searchInput = ref("");
const keyword = ref("");
/** 日志正文当前页（1 基）。服务端分页：每页 LOG_PAGE_SIZE 行，只渲染当前页。 */
const logPage = ref(1);
/** 级别档用户选择："" = 跟随服务端默认（首屏不下发 level）；切换后为显式档位 */
const levelOverride = ref("");
const autoRefresh = ref(true);
const logBox = ref<PreElement | null>(null);
const signTable = ref<TableState>({ page: 1, size: 20, prop: "time", order: "descending" });
const probeTable = ref<TableState>({ page: 1, size: 20, prop: "time", order: "descending" });
/** 运行巡检：轮级摘要与时间线（独立于日志请求，失败不拖垮日志分区） */
const runPayload = ref<RunEventsPayload | null>(null);
const runError = ref("");
const runLoading = ref(false);
const selectedKey = ref("");

/** 执行体健康卡（第二刀 ④）：独立于其它两块的请求，失败不拖垮整页。 */
const execPayload = ref<ExecHealthPayload | null>(null);
const execError = ref("");
const execLoading = ref(false);
/** 审计链自洽状态块（第二刀 ⑤）：只读服务端缓存快照（不重算全表哈希）。 */
const auditPayload = ref<AuditStatusPayload | null>(null);
const auditError = ref("");
const auditLoading = ref(false);

/** 实时进度流（SSE，`GET /api/stream/progress`）：挂在「运行巡检」分区顶部。
 *  本页仅管理员可达（pages.py 的 _admin_page_redirect），故流端点不会对普通用户开。
 *  口径纪律：事件计数**不去重**（≠ 首页「今日成功账号」）；`finalize` 单独一列；
 *  最近事件只给节点与执行体标签，逐账号明细留在下方时间线。 */
const liveState = ref<StreamState>(emptyStreamState());
const liveConn = ref<"connecting" | "open" | "closed">("connecting");
/** `end` 事件的原因（机器可读串）；空串 = 尚未收到 end */
const liveEndReason = ref("");
/** 流不可用（终止原因 / 握手被拒）：降级到既有 10 秒轮询，本块显示降级文案 */
const liveFallback = ref(false);
/** 最近一次收到 progress 事件的**本地**时刻：年龄按本地钟差算，不解析服务端时刻（免时区偏差） */
const liveLastRxMs = ref(0);
const liveNowMs = ref(Date.now());
let stream: EventSource | null = null;

let busy = false;
let runBusy = false;
/** 在途巡检请求期间到达的请求（合并：后到覆盖先到），不许静默丢弃 */
let runPending: { day: string; executor: string; keepSelection: boolean } | null = null;
let firstLoad = true;

const info = computed(() =>
  payload.value ? pagedInfoText(payload.value, (logPage.value - 1) * LOG_PAGE_SIZE) : "",
);
const logText = computed(() => (payload.value ? payload.value.logs.join("\n") : ""));
/** 日志正文总页数（空结果为 1，避免分页器出现 0 页）。 */
const logPageCount = computed(() =>
  payload.value ? Math.max(1, Math.ceil(payload.value.total_lines / LOG_PAGE_SIZE)) : 1,
);
const following = computed(() => viewDate.value === "");
const isToday = computed(() => payload.value?.is_today === true);
const canExport = computed(() => !!loadedDate.value);
const exportHref = computed(() => (loadedDate.value ? buildExportUrl(loadedDate.value) : ""));
const exportName = computed(() => payload.value?.log_file ?? "sign.log");
const collapsed = computed(() => payload.value?.collapsed_lines ?? 0);
/** 收起档开关的显示值：**由服务端回执校准**（`payload.level`），前端不内联档位取值。
 *  首屏响应到达前的挂起值只作视觉占位，不参与任何请求参数（首屏不下发 level）。 */
const warnOnly = ref(true);

const runRounds = computed<RunRound[]>(() => runPayload.value?.rounds ?? []);
const runEvents = computed<RunEvent[]>(() => runPayload.value?.events ?? []);
const selectedRound = computed(() => (runPayload.value ? findRound(runRounds.value, selectedKey.value) : null));
const runWindowLine = computed(() =>
  runPayload.value ? windowText(runPayload.value.window, runPayload.value.retention_days) : "",
);
const runEmptyText = computed(() => (runPayload.value ? emptyRoundsText(runPayload.value.window) : ""));
const runTruncatedLine = computed(() => (runPayload.value ? roundsTruncatedText(runPayload.value) : ""));
const timelineTitle = computed(() =>
  selectedRound.value ? `事件时间线 · ${selectedRound.value.day} · ${selectedRound.value.executor_label}` : "事件时间线",
);

/* 执行体健康卡与审计链状态块的**渲染派生**全部住在各自的纯函数模块里
   （`./exec-health` / `./audit-status`，有 Vitest 单测）：本组件只做取值与插值，
   不在模板里另写一份身份/新鲜度/三态的判据。 */
const execCards = computed<ExecHealthCard[]>(() => execPayload.value?.executors ?? []);
const execSummaryLine = computed(() =>
  execPayload.value ? healthSummary(execCards.value) : "",
);
const auditToneClass = computed(() =>
  auditPayload.value ? `badge--${auditTone(auditPayload.value.state)}` : "badge--muted",
);
const auditStateLine = computed(() =>
  auditPayload.value ? auditStateText(auditPayload.value.state) : "",
);
const auditAnchorLine = computed(() =>
  auditPayload.value ? anchorText(auditPayload.value) : "",
);
const auditDataAtLine = computed(() =>
  auditPayload.value ? dataAtText(auditPayload.value) : "",
);
const auditIndeterminateLine = computed(() =>
  auditPayload.value ? indeterminateText(auditPayload.value) : "",
);
const auditDebtLines = computed(() =>
  auditPayload.value ? knownDebtLines(auditPayload.value) : [],
);
const auditDetailLine = computed(() =>
  auditPayload.value ? auditDetailText(auditPayload.value) : "",
);

const liveConnected = computed(() => liveConn.value === "open" && !liveFallback.value);
const liveStatusLine = computed(() =>
  liveFallback.value
    ? "— 实时流不可用，已回退 10 秒轮询"
    : streamStatusText(liveConn.value, liveEndReason.value),
);
const liveMetaLine = computed(() => windowLine(liveState.value));
const liveAgeText = computed(() => {
  if (!liveLastRxMs.value) return "";
  const sec = Math.max(0, Math.floor((liveNowMs.value - liveLastRxMs.value) / 1000));
  return `距最近事件 ${sec} 秒`;
});
const liveNodeOrder = computed(() => liveState.value.nodeOrder);
const liveNodeLabels = computed(() => liveState.value.nodeLabels);
const liveExecs = computed(() => liveState.value.execs);
const liveRecentText = computed(() => recentText(liveState.value.recent));

/** 六节点某一列的计数（`node_order` 里的键在 `totals` 上必有；缺则回 0，不抛）。 */
function liveTotalOf(node: string): number {
  const totals = liveState.value.totals as unknown as Record<string, number>;
  return Number(totals[node] ?? 0);
}

function tableRows(events: LogEvent[], t: TableState): LogEvent[] {
  return paginate(sortEvents(events, t.prop, t.order), t.page, t.size);
}
const signRows = computed(() => tableRows(payload.value?.sign_events ?? [], signTable.value));
const probeRows = computed(() => tableRows(payload.value?.probe_events ?? [], probeTable.value));

function onSort(table: { value: TableState }, e: { prop: string | null; order: string | null }): void {
  if (!e.prop || !e.order) return;
  table.value = { ...table.value, prop: e.prop as TableState["prop"], order: e.order as TableState["order"], page: 1 };
}
function onSignSort(e: { prop: string | null; order: string | null }): void {
  onSort(signTable, e);
}
function onProbeSort(e: { prop: string | null; order: string | null }): void {
  onSort(probeTable, e);
}

/** 浮层打开时不轮询（与 legacy pollTick 一致：模态/下拉会遮挡阅读） */
function overlayOpen(): boolean {
  return !!document.querySelector(".pm-backdrop") || !!document.querySelector(".dd-wrap.is-open");
}

function writeUrlDate(date: string): void {
  try {
    const u = new URL(location.href);
    if (date) u.searchParams.set("date", date);
    else u.searchParams.delete("date");
    history.replaceState(null, "", u.pathname + u.search + u.hash); // 不 pushState：不堆历史
  } catch {
    /* 无 history/URL 的环境静默降级 */
  }
}

function readUrlDate(): string {
  try {
    const v = new URLSearchParams(location.search).get("date") || "";
    return isValidDate(v) ? v : "";
  } catch {
    return "";
  }
}

/** 替换文本**之前**测距：判断是否"接近底部"（首载或接近底部才自动滚到底，不打断向上翻阅）。 */
function nearBottom(): boolean {
  const box = logBox.value;
  if (!box) return true;
  return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
}

/** 日志正文某一页的查询串（1 基页码 → offset）。 */
function logPageQuery(date: string, page: number): string {
  return buildLogsQuery(
    date,
    keyword.value,
    false,
    levelOverride.value,
    LOG_PAGE_SIZE,
    (page - 1) * LOG_PAGE_SIZE,
  );
}

async function fetchLogPage(date: string, page: number): Promise<LogsPayload> {
  return api<LogsPayload>("GET", logPageQuery(date, page));
}

async function load(mode: "nav" | "poll" = "nav"): Promise<void> {
  if (busy) return;
  busy = true;
  loading.value = true;
  errorText.value = "";
  const wasNearBottom = nearBottom();
  const previousDate = loadedDate.value;
  // 导航请求用用户 pin 的日期（可能为空 = 跟随最新）；轮询刷新沿用**已加载的那一天**，
  // 否则跟随态下服务端会重新解析日期、把用户正在看的内容换掉。
  const date = mode === "poll" ? loadedDate.value : viewDate.value;
  try {
    // 先取第一页只为拿 total_lines，再直接取最后一页：首屏落的永远是**最新**一页，
    // 不会先渲染旧页再跳。分页后服务端只回当前页，DOM 行数不再随当日总量增长。
    const head = await fetchLogPage(date, 1);
    const last = Math.max(1, Math.ceil(head.total_lines / LOG_PAGE_SIZE));
    const resp = last === 1 ? head : await fetchLogPage(date, last);
    payload.value = resp;
    logPage.value = last;
    loadedDate.value = resp.date;
    dateInput.value = resp.date;
    // 档位显示校准：以服务端回执为准（不把默认档位抄进前端）
    warnOnly.value = resp.level !== ALL_LOG_LEVEL;
    for (const t of [signTable.value, probeTable.value]) t.page = 1;
    await nextTick();
    const box = logBox.value;
    if (box && (firstLoad || wasNearBottom)) box.scrollTop = box.scrollHeight;
    firstLoad = false;
  } catch (e) {
    errorText.value = (e as Error)?.message || "网络错误，请重试";
  } finally {
    busy = false;
    loading.value = false;
  }
  // 运行巡检块跟随日志分区的**实际日期**：两块内容始终同一天。失败只影响本块。
  // 轮询与同日重载**沿用在途选择**（10 秒一次不得把用户点开的旧轮改回最新）；
  // 日期变了才回落该日最新一轮。
  const keepSelection = mode === "poll" || loadedDate.value === previousDate;
  await loadRuns(loadedDate.value, "", keepSelection);
  // 健康卡与 10 秒轮询同节拍（它回答"现在在不在跑"，越新越有用）；
  // 审计体检快照一天只写一次，只在导航/首屏取，不必每 10 秒拉一次。
  if (mode !== "poll") void loadAuditStatus();
  void loadExecHealth();
}

/**
 * 拉运行巡检（轮级摘要 + 选中轮的时间线）。
 *
 * `executor` 为空时由服务端取该日最新一轮，并同时回该轮时间线。
 * `keepSelection` 为真时沿用在途选择（轮询/同刷新路径），否则回落该日最新一轮。
 * 该端点与 `/api/logs` 是两个独立请求：巡检块失败只在块内报错，日志分区照常可用。
 *
 * 在途请求期间到达的请求**排队合并**（后到覆盖先到）：成功路径上不再静默丢弃。
 * 丢弃会让一次点击的导航请求消失，页面停在旧轮；合并后每一次最终状态都执行一遍。
 *
 * **错误路径丢弃在途排队请求**（catch 里清空 `runPending`）：请求失败时，在途期排队的
 * 点击被清掉，`selectedKey` 不更新，页面停旧轮。这可以接受：错误已由 `runError` 显式
 * 呈现，属**非静默**。此处不做自我重试。
 * 自愈只在一半情形成立：**跟随最新态下**，10 秒节拍的下一次轮询会重新发起请求；
 * **pin 住日期时不轮询**（`shouldPoll` 要求 `following === true`，pin 态 `following=false`），
 * 那次被丢弃的点击**会永久丢失**，直到用户再点一次。
 */
async function loadRuns(day: string, executor = "", keepSelection = false): Promise<void> {
  // 保留在途选择时，把选中轮的执行体一起带上。为什么必须带：不带 executor 时服务端回
  // 该日**最新**一轮的时间线，页面选中态与时间线于是不一致——这是"轮询把旧轮改回最新"
  // 的另一半根因（只保留 selectedKey 不够，取回的数据也必须是那一轮的）。
  if (keepSelection && !executor && selectedKey.value.startsWith(`${day}|`)) {
    executor = selectedKey.value.slice(day.length + 1);
  }
  if (runBusy) {
    runPending = { day, executor, keepSelection };
    return;
  }
  runBusy = true;
  runLoading.value = true;
  runError.value = "";
  let req = { day, executor, keepSelection };
  // R2：只允许一次"去掉 executor 的重取"，避免重取环。
  let executorFallbackUsed = false;
  try {
    for (;;) {
      const resp = await api<RunEventsPayload>("GET", buildRunEventsQuery(req.day, req.executor));
      // R2：带了 executor 但响应 rounds 不含该执行体分组时，选中态与时间线不同源
      //（标题取该日最新轮、时间线该执行体却为空）。不带 executor 重取一次，让服务端
      // 重新选定该日最新一轮，两块回到同源。只允许一次，不许成环。
      if (!executorFallbackUsed &&
          needsExecutorFallback(resp.rounds, resp.window.day, req.executor)) {
        executorFallbackUsed = true;
        req = { day: req.day, executor: "", keepSelection: false };
        continue;
      }
      runPayload.value = resp;
      selectedKey.value = nextSelectedKey(
        resp.rounds, resp.window.day, req.executor, selectedKey.value, req.keepSelection,
      );
      const next = runPending;
      runPending = null;
      if (!next) break;
      req = next;
    }
  } catch (e) {
    runError.value = (e as Error)?.message || "运行进度加载失败";
    runPending = null;
  } finally {
    runBusy = false;
    runLoading.value = false;
  }
}

function selectRound(row: RunRound): void {
  const key = roundKey(row);
  if (key === selectedKey.value) return;
  void loadRuns(row.day, row.executor);
}

function refreshRuns(): void {
  void loadRuns(runPayload.value?.window.day || loadedDate.value, "", true);
}

/**
 * 拉执行体健康卡（第二刀 ④）。三块（时间线 / 健康卡 / 审计链）各自独立请求：
 * 任一块失败只在该块内报错，不拖垮其它块——巡检页最需要的是"其余部分照常可用"。
 */
async function loadExecHealth(): Promise<void> {
  if (execLoading.value) return;
  execLoading.value = true;
  execError.value = "";
  try {
    execPayload.value = await api<ExecHealthPayload>("GET", execHealthQuery());
  } catch (e) {
    execError.value = (e as Error)?.message || "执行体健康卡加载失败";
  } finally {
    execLoading.value = false;
  }
}

/**
 * 拉审计链自洽状态块（第二刀 ⑤）。服务端只回**缓存的体检快照**（每日自检写入），
 * 本请求不触发全表哈希重算；结果自带数据时刻，页面照实显示。
 */
async function loadAuditStatus(): Promise<void> {
  if (auditLoading.value) return;
  auditLoading.value = true;
  auditError.value = "";
  try {
    auditPayload.value = await api<AuditStatusPayload>("GET", auditStatusQuery());
  } catch (e) {
    auditError.value = (e as Error)?.message || "审计链状态加载失败";
  } finally {
    auditLoading.value = false;
  }
}

/** 巡检块的三块一起刷新（「刷新进度」按钮与首屏用）。 */
function refreshInspection(): void {
  refreshRuns();
  void loadExecHealth();
  void loadAuditStatus();
}

/** SSE 帧 → JSON（非字符串 / 非法 JSON 回 null，一次坏帧不得打断状态机）。 */
function parseFrame<T>(e: Event): T | null {
  const raw = (e as MessageEvent).data;
  if (typeof raw !== "string" || !raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

function stopStream(): void {
  if (stream) {
    stream.close();
    stream = null;
  }
}

/**
 * 接进度流。自动重连与 `Last-Event-ID` 续传由浏览器原生 `EventSource` 承担。
 *
 * 降级判据两条（满足任一即回退既有 10 秒轮询）：
 * ① `end` 的终止原因（会话过期 / 开关关闭 / 连接超限 / 服务繁忙）——重连只会再被拒；
 * ② `error` 且 `readyState=CLOSED`（握手被拒，如 401/403/内容类型不符）——不可恢复。
 * 其余 `end` 原因（时限 / 空闲）与瞬断交给浏览器自动重连，本函数不拦。
 */
function startStream(): void {
  stopStream();
  liveEndReason.value = "";
  liveFallback.value = false;
  liveConn.value = "connecting";
  let es: EventSource;
  try {
    // 子路径部署（BasePathMiddleware）下必须带 `shellBase()` 前缀，与 `api()` 同口径；
    // 根路径形态下 shellBase() 为空串，URL 不变。
    es = new EventSource(shellBase() + "/api/stream/progress");
  } catch {
    liveFallback.value = true;
    liveConn.value = "closed";
    return;
  }
  stream = es;
  es.addEventListener("open", () => {
    liveConn.value = "open";
  });
  es.addEventListener("snapshot", (e) => {
    const snap = parseFrame<StreamSnapshot>(e);
    if (snap) liveState.value = stateFromSnapshot(snap);
    liveConn.value = "open";
  });
  es.addEventListener("progress", (e) => {
    const ev = parseFrame<StreamProgress>(e);
    if (ev) {
      liveState.value = applyProgress(liveState.value, ev);
      liveLastRxMs.value = Date.now();
    }
    liveConn.value = "open";
  });
  es.addEventListener("heartbeat", () => {
    liveConn.value = "open";
  });
  es.addEventListener("resync", () => {
    /* 后紧跟一个 snapshot：等它整体替换，此处不动状态 */
  });
  es.addEventListener("end", (e) => {
    liveEndReason.value = parseFrame<{ reason?: string }>(e)?.reason ?? "";
    if (isTerminalEnd(liveEndReason.value)) {
      stopStream();
      liveConn.value = "closed";
      liveFallback.value = true;
    }
  });
  es.addEventListener("error", () => {
    if (!stream) return;
    if (es.readyState === EventSource.CLOSED) {
      liveEndReason.value = "";
      liveConn.value = "closed";
      liveFallback.value = true;
      stopStream();
    } else {
      liveConn.value = "connecting";
    }
  });
}

function search(): void {
  keyword.value = searchInput.value.trim();
  void load();
}

/**
 * 组合输入（中文输入法）进行中。
 *
 * 为什么需要一个显式标志：组合期的那次回车是「提交候选词」，浏览器**仍会对表单做隐式提交**
 * （实测 Chromium：keydown 的 isComposing 为真时 submit 照样发生一次）。只在 keydown 里
 * `if (isComposing) return` 挡不住它，提交照样进 search()，发出去的是**未提交**的拼音串。
 * 故把守卫放在提交路径上，并保证组合期不 preventDefault（不打断输入法）。
 */
const composing = ref(false);

/**
 * 表单提交（点「检索」，以及回车走到的那次隐式提交）。
 *
 * 组合期直接返回：这次提交是输入法提交候选词带来的，不是检索指令。
 * 注意 `.prevent` 由模板编译器内联在处理器之前执行，所以这里早退也不会发生原生 GET 重载。
 */
function submitSearch(): void {
  if (composing.value) return;
  search();
}

/**
 * 检索框里按下回车。
 *
 * 组合期交给输入法（直接返回，也不 preventDefault）；非组合期挡掉隐式提交再发起检索——
 * 挡掉是为了让一次回车只发一次请求（否则 keydown 与隐式提交会各发一次；`busy` 守卫虽能
 * 兜住，但那条依赖实现细节，不该当作唯一保障）。
 */
function onSearchEnter(e: KeyboardEvent): void {
  if (e.isComposing || composing.value) return;
  e.preventDefault();
  search();
}

/** 「查看该日」：空值与非法值各自提示（与 legacy 文案一致） */
function viewDateAction(): void {
  const v = dateInput.value.trim();
  if (!v) {
    toast().error("请先选择日期");
    return;
  }
  if (!isValidDate(v)) {
    toast().error("日期格式不正确，应为 YYYY-MM-DD");
    return;
  }
  viewDate.value = v;
  writeUrlDate(v);
  void load();
}

function jumpToRecent(date: string): void {
  if (!date) return;
  viewDate.value = date;
  dateInput.value = date;
  writeUrlDate(date);
  void load();
}

/** 回到跟随态（只复位日期：legacy 的「回到今天」不动检索与「显示全部」） */
function backToToday(): void {
  viewDate.value = "";
  dateInput.value = "";
  writeUrlDate("");
  void load();
}

/* el-date-picker 清空时回 null；归一成 "" 与旧原生 input 的空值口径一致
   （viewDateAction 只认 "YYYY-MM-DD"，空值走「请先选择日期」提示）。 */
function onDatePick(v: string | null): void {
  dateInput.value = v ?? "";
}

/** 翻到日志正文某一页：只重拉 /api/logs 当前页，不重拉运行巡检/健康卡/审计状态。 */
async function onLogPageChange(p: number): Promise<void> {
  if (busy || !loadedDate.value) return;
  const target = Math.max(1, Math.floor(p) || 1);
  if (target === logPage.value) return;
  busy = true;
  loading.value = true;
  errorText.value = "";
  try {
    let resp = await fetchLogPage(loadedDate.value, target);
    const maxPage = Math.max(1, Math.ceil(resp.total_lines / LOG_PAGE_SIZE));
    if (target > maxPage) {
      // 请求期间当日行数变少：夹回最后一页，不留在空页上。
      resp = await fetchLogPage(loadedDate.value, maxPage);
    }
    payload.value = resp;
    logPage.value = Math.min(target, maxPage);
    await nextTick();
    if (logBox.value) logBox.value.scrollTop = 0;
  } catch (e) {
    errorText.value = (e as Error)?.message || "网络错误，请重试";
  } finally {
    busy = false;
    loading.value = false;
  }
}

/** 级别档开关：切档后重载（服务端按档过滤，前端不自筛）。切换时才显式下发档位。 */
function onLevelChange(): void {
  levelOverride.value = warnOnly.value ? WARN_LOG_LEVEL : ALL_LOG_LEVEL;
  void load();
}

/** 空态的「显示全部级别」：把收起档切到全量档。 */
function showAllLevels(): void {
  levelOverride.value = ALL_LOG_LEVEL;
  void load();
}

function onAutoRefreshChange(): void {
  try {
    localStorage.setItem(AUTO_KEY, autoRefresh.value ? "1" : "0");
  } catch {
    /* 受限环境忽略 */
  }
}

function tick(): void {
  if (
    shouldPoll({
      visibility: document.visibilityState,
      autoRefresh: autoRefresh.value,
      following: following.value,
      busy,
      overlayOpen: overlayOpen(),
      atLatest: logPage.value >= logPageCount.value,
    })
  ) {
    void load("poll");
  }
}
/* 日志列表轮询：机制（节拍 / 可见性 / 卸载清理）在 `lib/polling.ts`，
   本页只给策略（`shouldPoll`：自动刷新开、处于跟随态、无在途请求、无浮层）。
   节拍取外壳唯一来源 `pollMs()` —— 收编前这里自带一份节拍常量，
   与账号页/用户页/总览 KPI 是四处独立常量。 */
usePolling(tick, { intervalMs: pollMs() });
// 进度流的"最近收到事件"年龄：每秒推进本地钟，走同一机制（1 秒档）。
usePolling(() => { liveNowMs.value = Date.now(); }, { intervalMs: 1000 });

const tabParam = new URLSearchParams(location.search).get("tab");
function syncTabUrl(next: TabKey): void {
  const params = new URLSearchParams(location.search);
  params.set("tab", next);
  history.replaceState(null, "", `${location.pathname}?${params.toString()}${location.hash}`);
}
watch(tab, syncTabUrl);

onMounted(() => {
  if (tabParam && (TAB_KEYS as string[]).includes(tabParam)) tab.value = tabParam as TabKey;
  try {
    const saved = localStorage.getItem(AUTO_KEY);
    if (saved === "0") autoRefresh.value = false;
  } catch {
    /* 受限环境忽略 */
  }
  const urlDate = readUrlDate();
  if (urlDate) {
    viewDate.value = urlDate;
    dateInput.value = urlDate;
  }
  void load();
  // 实时流自带重连节拍；年龄心跳走 usePolling（与冷却倒计时同一个机制），
  // 故本页不再自带任何定时器。
  startStream();
});

onBeforeUnmount(() => {
  stopStream();
});
</script>

<template>
  <div class="logs">
    <el-tabs v-model="tab">
      <el-tab-pane label="日志" name="main">
        <!-- 签到日志：本页第一块。运行巡检降为下一块，避免用户进「签到日志」先看到
             四段诊断信息、把真正的日志推到第二屏。 -->
        <div class="card">
          <div class="logs-head logs-head--log">
            <h2 class="logs-title">签到日志 <span class="logs-file">{{ payload?.log_file ?? "" }}</span></h2>
            <div class="logs-tools">
              <!-- 检索是一段**表单**：移动端键盘的「搜索/前往」键只有落到表单提交才有去处
                   （原来只有 @keyup.enter + 裸按钮，那个键按下去什么也不发生）。
                   回车另有输入框上的 keydown 处理：检索按钮在 loading 期间是 disabled，而
                   隐式提交要求默认按钮可用——只留表单会在 loading 窗口把回车变成静默无效。
                   组合输入（中文输入法）期的回车不检索，由 form 上的 composing 标志挡住那次
                   隐式提交（浏览器不为 isComposing 跳过隐式提交，只挡 keydown 是不够的）。 -->
              <form
                class="logs-search"
                role="search"
                aria-label="检索日志"
                @submit.prevent="submitSearch"
                @compositionstart="composing = true"
                @compositionend="composing = false"
              >
                <label class="sr-only" for="logs-search-input">检索日志关键字</label>
                <input
                  id="logs-search-input"
                  v-model="searchInput"
                  class="input"
                  type="search"
                  placeholder="检索日志关键字"
                  @keydown.enter="onSearchEnter"
                />
                <button type="submit" class="btn btn--ghost btn--sm" :disabled="loading">检索</button>
              </form>
              <div class="logs-actions">
                <a v-if="canExport" class="btn btn--ghost btn--sm" :href="exportHref" :download="exportName" role="button">导出日志</a>
                <button v-else type="button" class="btn btn--ghost btn--sm" disabled title="尚未加载出可导出的日期">导出日志</button>
              </div>
            </div>
            <!-- 状态文本是「看不见的事实」类文案，不是控件。它独立成次要行、落在控件行之下，
                 不与按钮抢同一行——否则右组超宽时它折到第二行，标题（align-items:center）
                 会对齐两行整块，与控件行错位（2026-10-11 结构修）。 -->
            <span class="logs-info" role="status">{{ loading ? "正在加载日志…" : info }}</span>
          </div>

          <div class="logs-datebar">
            <label class="logs-field">
              <span class="logs-label">查看日期</span>
              <!-- 日期也走 Element Plus：原生 input[type=date] 的日历弹层由 UA 渲染、样式不可控。
                   value-format 保持 "YYYY-MM-DD"，与 isValidDate / URL 深链口径逐字一致；
                   清空得到 null，onDatePick 归一成 ""。 -->
              <el-date-picker
                :model-value="dateInput || null"
                type="date"
                format="YYYY-MM-DD"
                value-format="YYYY-MM-DD"
                placeholder="选择日期"
                aria-label="查看日期"
                class="logs-date"
                @update:model-value="onDatePick"
              />
            </label>
            <div class="logs-date-actions">
              <button type="button" class="btn btn--ghost btn--sm" :disabled="loading" @click="viewDateAction">查看该日日志</button>
              <button v-if="payload && !isToday" type="button" class="btn btn--ghost btn--sm" @click="backToToday">回到今天</button>
            </div>
            <!-- 级别档开关：默认收起 INFO（巡检只看 WARN／ERROR）。类名刻意与
                 `.logs-check`（自动刷新）分开——两处同用一个类会让既有 e2e 选择器命中两个。 -->
            <div class="logs-filters">
              <label class="logs-level">
                <input id="logs-level-warn" v-model="warnOnly" type="checkbox" @change="onLevelChange" />
                <span>只看告警（收起 INFO）</span>
              </label>
              <label class="logs-check">
                <input v-model="autoRefresh" type="checkbox" @change="onAutoRefreshChange" />
                <span>自动刷新（10 秒，仅跟随最新时）</span>
              </label>
            </div>
          </div>

          <el-alert v-if="errorText" type="error" :title="errorText" :closable="false" show-icon style="margin: 8px 0" />
          <div v-if="errorText" class="logs-retry">
            <button type="button" class="btn btn--ghost btn--sm" :disabled="loading" @click="load()">重试</button>
          </div>
          <pre v-show="logText" ref="logBox" class="log-box" role="log" aria-live="off">{{ logText }}</pre>
          <div
            v-if="payload && payload.total_lines > LOG_PAGE_SIZE"
            class="logs-logpager"
            data-log-pager
          >
            <el-pagination
              :current-page="logPage"
              :page-size="LOG_PAGE_SIZE"
              :total="payload.total_lines"
              :pager-count="5"
              layout="prev, pager, next"
              small
              @current-change="onLogPageChange"
            />
          </div>
          <div v-if="!logText && !loading && !errorText" id="logs-empty" class="logs-empty">
            <p v-if="keyword">（无匹配日志行）</p>
            <p v-else-if="collapsed > 0">
              （当前档只显示 WARN／ERROR，已收起 {{ collapsed }} 行 INFO／DEBUG）
              <button type="button" class="btn btn--ghost btn--sm" @click="showAllLevels">显示全部级别</button>
            </p>
            <p v-else>
              {{ viewDate ? `（${viewDate} 无签到日志）` : "（暂无签到日志，等待定时任务执行…）" }}
              <button
                v-if="payload?.recent_log_date"
                type="button"
                class="btn btn--ghost btn--sm"
                @click="jumpToRecent(payload.recent_log_date)"
              >
                查看 {{ payload.recent_log_date }}
              </button>
            </p>
          </div>
        </div>


      </el-tab-pane>

      <el-tab-pane label="运行巡检" name="inspect">
        <!-- 运行巡检：轮级摘要一眼看完，点某轮展开该轮时间线。 -->
        <div id="run-panel" class="card">
          <div class="logs-head">
            <h2 class="logs-title">运行巡检</h2>
            <div class="logs-tools">
              <button type="button" class="btn btn--ghost btn--sm" :disabled="runLoading" @click="refreshInspection">
                刷新进度
              </button>
              <span id="run-window" class="run-info">{{ runWindowLine }}</span>
            </div>
          </div>

          <!-- 运行进度（实时）：SSE 流（bd fdjl / 125 §4.2）。一屏两带：全局六节点总数
               + 每执行体一行 + 最近事件。两条口径纪律：事件计数**不去重**（≠ 首页
               「今日成功账号」）；`finalize` 单独一列。最近事件只给节点与执行体标签，
               逐账号明细留在下方时间线。流不可用时本块显示降级文案，10 秒轮询照常。 -->
          <div id="run-live" class="run-live">
            <div class="run-live-head">
              <span
                id="live-status"
                class="run-live-status"
                :class="liveConnected ? 'is-on' : 'is-off'"
                role="status"
              >{{ liveStatusLine }}</span>
              <span id="live-meta" class="run-info">{{ liveMetaLine }}</span>
              <span v-if="liveAgeText" id="live-age" class="run-info">{{ liveAgeText }}</span>
            </div>
            <div v-if="liveNodeOrder.length" id="live-totals" class="live-totals">
              <div v-for="n in liveNodeOrder" :key="n" class="live-total">
                <span class="live-total-label">{{ liveNodeLabels[n] ?? n }}</span>
                <span class="live-total-value">{{ liveTotalOf(n) }}</span>
              </div>
            </div>
            <p v-else class="run-note">等待实时进度流…</p>
            <p class="run-note">注：以上为 run_events 事件计数（不去重），与首页「今日成功账号」（按账号去重）不是同一口径。</p>
            <div class="run-table-wrap">
              <el-table v-if="liveExecs.length" id="live-execs" :data="liveExecs" size="small">
                <el-table-column prop="executor" label="执行体" width="130" />
                <el-table-column label="最近活动" width="110">
                  <template #default="{ row }">{{ eventTime(row.last_ts) }}</template>
                </el-table-column>
                <el-table-column prop="claim" label="领取" width="70" />
                <el-table-column prop="start" label="开始" width="70" />
                <el-table-column prop="success" label="成功事件" width="90" />
                <el-table-column prop="fail" label="失败" width="70" />
                <el-table-column prop="pause" label="暂停" width="70" />
                <el-table-column prop="finalize" label="收尾" width="70" />
                <el-table-column prop="unexecuted" label="未执行" width="80" />
                <el-table-column label="耗时" width="90">
                  <template #default="{ row }">{{ formatDuration(row.duration_sec) }}</template>
                </el-table-column>
                <el-table-column label="状态" min-width="110">
                  <template #default="{ row }">{{ execRowNote(row) }}</template>
                </el-table-column>
              </el-table>
            </div>
            <div v-if="liveExecs.length" class="run-mobile-cards" aria-label="实时执行体">
              <article v-for="row in liveExecs" :key="'live-' + row.executor" class="run-mobile-card">
                <div class="run-mobile-top">
                  <strong>{{ row.executor }}</strong>
                  <span class="run-info">{{ eventTime(row.last_ts) }}</span>
                </div>
                <div class="run-mobile-meta">领取 {{ row.claim }} · 开始 {{ row.start }} · 成功 {{ row.success }} · 失败 {{ row.fail }}</div>
                <div class="run-mobile-meta">暂停 {{ row.pause }} · 收尾 {{ row.finalize }} · 未执行 {{ row.unexecuted }} · {{ formatDuration(row.duration_sec) }}</div>
                <div class="run-mobile-meta">{{ execRowNote(row) }}</div>
              </article>
            </div>
            <div id="live-recent" class="live-recent">
              <span class="live-recent-label">最近事件（不含账号）</span>
              <span class="live-recent-text">{{ liveRecentText }}</span>
            </div>
          </div>

          <!-- 执行体健康卡（第二刀 ④，工单 o3o7）：每个执行体一行，回答心跳新鲜度、
               最后活动时刻、吞吐。身份由服务端按**心跳文件名**解析（记录体自称的
               role/index 不作数）；心跳过期或无记录都显式写「无心跳」，不留白。 -->
          <div id="exec-health" class="run-block">
            <h3 class="run-sub">执行体健康</h3>
            <p v-if="execError" class="run-note">{{ execError }}</p>
            <template v-else>
              <p id="exec-health-summary" class="run-info">{{ execSummaryLine }}</p>
              <div class="run-table-wrap">
                <el-table v-if="execCards.length" id="exec-health-table" :data="execCards" size="small">
                  <el-table-column label="执行体" min-width="150">
                    <template #default="{ row }">
                      <span class="exec-name">{{ cardName(row) }}</span>
                      <span
                        v-if="unconfiguredNote(row)"
                        id="exec-health-unconfigured"
                        class="exec-flag"
                      >{{ unconfiguredNote(row) }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="身份" width="120">
                    <template #default="{ row }"><span class="exec-ident">{{ row.identity }}</span></template>
                  </el-table-column>
                  <el-table-column label="状态" width="110">
                    <template #default="{ row }">
                      <span class="badge" :class="`badge--${execStateTone(row.state)}`">
                        {{ execStateText(row.state) }}
                      </span>
                    </template>
                  </el-table-column>
                  <el-table-column label="心跳 / 最后活动" min-width="230">
                    <template #default="{ row }">{{ heartbeatText(row) }}</template>
                  </el-table-column>
                  <el-table-column label="吞吐" min-width="200">
                    <template #default="{ row }">{{ throughputText(row) }}</template>
                  </el-table-column>
                </el-table>
              </div>
              <div v-if="execCards.length" class="run-mobile-cards" aria-label="执行体健康">
                <article v-for="row in execCards" :key="'health-' + row.identity" class="run-mobile-card">
                  <div class="run-mobile-top">
                    <strong>{{ cardName(row) }}</strong>
                    <span class="badge" :class="`badge--${execStateTone(row.state)}`">{{ execStateText(row.state) }}</span>
                  </div>
                  <div class="run-mobile-meta">
                    {{ row.identity }}
                    <span v-if="unconfiguredNote(row)"> · {{ unconfiguredNote(row) }}</span>
                  </div>
                  <div class="run-mobile-meta">{{ heartbeatText(row) }}</div>
                  <div class="run-mobile-meta">{{ throughputText(row) }}</div>
                </article>
              </div>
              <p id="exec-health-caliber" class="run-note">
                吞吐口径：分子＝当日该执行体名下**已了结**的账号数（任务台账按执行体分组，一账号一行）；
                分母＝有效签到窗口长度。它与上方「运行进度」的事件计数（不去重）不是同一口径。
                身份以心跳文件名编码的档位为准；心跳记录体自称的角色/槽位一律不采。
              </p>
            </template>
          </div>

          <!-- 审计链自洽状态块（第二刀 ⑤，工单 vjy9）：三态分开显示——健康 / 检出篡改 /
               无法定论；「判不出来」绝不当成健康，也不静默略过。已知欠账单独一行、用中性色
               （不与「篡改」同色）。只读服务端缓存快照，页面不触发全表哈希重算。 -->
          <div id="audit-chain" class="run-block">
            <h3 class="run-sub">审计链自洽</h3>
            <p v-if="auditError" class="run-note">{{ auditError }}</p>
            <template v-else>
              <p class="audit-line">
                <span id="audit-state" class="badge" :class="auditToneClass">{{ auditStateLine }}</span>
                <span id="audit-anchor" class="run-info">{{ auditAnchorLine }}</span>
                <span id="audit-data-at" class="run-info">{{ auditDataAtLine }}</span>
              </p>
              <p v-if="auditIndeterminateLine" id="audit-indeterminate" class="run-note">
                {{ auditIndeterminateLine }}
              </p>
              <ul v-if="auditDebtLines.length" id="audit-known-debt" class="audit-debt">
                <li v-for="(line, i) in auditDebtLines" :key="i">{{ line }}</li>
              </ul>
              <p v-if="auditDetailLine" id="audit-detail" class="run-note">{{ auditDetailLine }}</p>
            </template>
          </div>

          <el-alert v-if="runError" type="error" :title="runError" :closable="false" show-icon style="margin: 8px 0" />
          <div v-if="runError" class="logs-retry">
            <button type="button" class="btn btn--ghost btn--sm" :disabled="runLoading" @click="refreshRuns">重试</button>
          </div>

          <div v-if="!runRounds.length" id="run-empty" class="run-empty">
            <p>{{ runLoading ? "正在加载运行进度…" : runEmptyText }}</p>
          </div>
          <template v-else>
            <div id="run-summary">
              <div class="run-table-wrap">
                <el-table :data="runRounds" size="small" @row-click="selectRound">
                  <el-table-column prop="day" label="业务日" width="120" />
                  <el-table-column prop="executor_label" label="执行体" width="130" />
                  <el-table-column prop="claim" label="领取" width="80" />
                  <el-table-column prop="success" label="成功" width="80" />
                  <el-table-column prop="fail" label="失败" width="80" />
                  <el-table-column prop="unexecuted" label="未执行" width="90" />
                  <el-table-column label="耗时" width="110">
                    <template #default="{ row }">{{ formatDuration(row.duration_sec) }}</template>
                  </el-table-column>
                  <el-table-column label="选中" width="90">
                    <template #default="{ row }">
                      <span v-if="roundKey(row) === selectedKey" class="run-current">当前</span>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
              <div class="run-mobile-cards" aria-label="轮次摘要">
                <button
                  v-for="row in runRounds"
                  :key="'round-' + roundKey(row)"
                  type="button"
                  class="run-mobile-card run-mobile-card--button"
                  :class="{ 'is-current': roundKey(row) === selectedKey }"
                  @click="selectRound(row)"
                >
                  <span class="run-mobile-top">
                    <strong>{{ row.day }} · {{ row.executor_label }}</strong>
                    <span v-if="roundKey(row) === selectedKey" class="run-current">当前</span>
                  </span>
                  <span class="run-mobile-meta">领取 {{ row.claim }} · 成功 {{ row.success }} · 失败 {{ row.fail }} · 未执行 {{ row.unexecuted }}</span>
                  <span class="run-mobile-meta">{{ formatDuration(row.duration_sec) }}</span>
                </button>
              </div>
            </div>
            <p v-if="runTruncatedLine" id="run-summary-truncated" class="run-note">
              {{ runTruncatedLine }}
            </p>
            <h3 class="run-sub">{{ timelineTitle }}</h3>
            <div id="run-timeline">
              <div class="run-table-wrap">
                <el-table :data="runEvents" size="small">
                  <template #empty>
                    <div id="run-timeline-empty" class="run-empty">该轮没有可展开的事件行</div>
                  </template>
                  <el-table-column label="时刻" width="110">
                    <template #default="{ row }">{{ eventTime(row.ts) }}</template>
                  </el-table-column>
                  <el-table-column prop="node_label" label="节点" width="90" />
                  <el-table-column prop="phone" label="账号" width="150">
                    <!-- 服务端已遮（唯一出口）；前端不再自遮，直接渲染下发值 -->
                    <template #default="{ row }">{{ row.phone || "--" }}</template>
                  </el-table-column>
                  <el-table-column prop="message" label="说明" min-width="260" show-overflow-tooltip />
                </el-table>
              </div>
              <div v-if="runEvents.length" class="run-mobile-cards" aria-label="事件时间线">
                <article v-for="(row, i) in runEvents" :key="'timeline-' + i" class="run-mobile-card">
                  <div class="run-mobile-top">
                    <strong>{{ eventTime(row.ts) }}</strong>
                    <span class="run-info">{{ row.node_label }}</span>
                  </div>
                  <div class="run-mobile-meta">{{ row.phone || "--" }}</div>
                  <p class="run-mobile-msg">{{ row.message }}</p>
                </article>
              </div>
              <p v-else class="run-empty">该轮没有可展开的事件行</p>
              <p v-if="runPayload?.events_truncated" id="run-timeline-truncated" class="run-note">
                时间线已截断（本次最多 {{ runPayload.events_limit }} 行）。
              </p>
            </div>
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane name="signev">
        <template #label>签到事件 <span class="logs-count">{{ payload?.sign_events.length ?? 0 }}</span></template>
        <div class="card">
          <div class="logs-table-wrap">
            <el-table :data="signRows" size="small" @sort-change="onSignSort">
              <template #empty>
                <div class="logs-empty">
                  该日无签到事件
                  <button
                    v-if="payload?.recent_sign_date"
                    type="button"
                    class="btn btn--ghost btn--sm"
                    @click="jumpToRecent(payload.recent_sign_date)"
                  >
                    查看 {{ payload.recent_sign_date }}
                  </button>
                </div>
              </template>
              <el-table-column prop="time" label="时间" width="110" sortable="custom">
                <template #default="{ row }">{{ row.time || "--:--:--" }}</template>
              </el-table-column>
              <el-table-column prop="phone" label="账号" width="150">
                <!-- 服务端已遮（唯一出口）；前端不再自遮，直接渲染下发值 -->
                <template #default="{ row }">{{ row.phone }}</template>
              </el-table-column>
              <el-table-column prop="status" label="状态" width="130" sortable="custom">
                <template #default="{ row }">
                  <span
                    class="badge"
                    :class="`badge--${statusLabel('sign', row.status).tone}`"
                    :title="statusLabel('sign', row.status).raw"
                  >{{ statusLabel("sign", row.status).label }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="attempt" label="尝试" width="80" sortable="custom" />
              <el-table-column prop="message" label="说明" min-width="240" show-overflow-tooltip>
                <template #default="{ row }">{{ eventMessage(row, true) }}</template>
              </el-table-column>
            </el-table>
          </div>
          <div v-if="signRows.length" class="logs-event-cards" aria-label="签到事件">
            <article v-for="(row, i) in signRows" :key="'sign-' + i" class="logs-event-card">
              <div class="logs-event-top">
                <span class="logs-event-time">{{ row.time || "--:--:--" }}</span>
                <span class="badge" :class="`badge--${statusLabel('sign', row.status).tone}`" :title="statusLabel('sign', row.status).raw">
                  {{ statusLabel("sign", row.status).label }}
                </span>
              </div>
              <div class="logs-event-phone">{{ row.phone }}</div>
              <div class="logs-event-attempt">尝试 {{ row.attempt }} 次</div>
              <p class="logs-event-msg">{{ eventMessage(row, true) }}</p>
            </article>
          </div>
          <p v-else class="logs-empty logs-event-empty">
            该日无签到事件
            <button
              v-if="payload?.recent_sign_date"
              type="button"
              class="btn btn--ghost btn--sm"
              @click="jumpToRecent(payload.recent_sign_date)"
            >
              查看 {{ payload.recent_sign_date }}
            </button>
          </p>
          <div v-if="(payload?.sign_events.length ?? 0) > 0" class="logs-pager">
            <span class="logs-pager-total">{{ eventCountText(payload?.sign_events.length ?? 0) }}</span>
            <el-pagination
              v-model:current-page="signTable.page"
              v-model:page-size="signTable.size"
              :total="payload?.sign_events.length ?? 0"
              :page-sizes="EVENT_PAGE_SIZES"
              layout="total, sizes, prev, pager, next"
              @size-change="signTable.page = 1"
            />
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane name="probe">
        <template #label>探针记录 <span class="logs-count">{{ payload?.probe_events.length ?? 0 }}</span></template>
        <div class="card">
          <div class="logs-table-wrap">
            <el-table :data="probeRows" size="small" @sort-change="onProbeSort">
              <template #empty>
                <div class="logs-empty">
                  该日无探针记录
                  <button
                    v-if="payload?.recent_probe_date"
                    type="button"
                    class="btn btn--ghost btn--sm"
                    @click="jumpToRecent(payload.recent_probe_date)"
                  >
                    查看 {{ payload.recent_probe_date }}
                  </button>
                </div>
              </template>
              <el-table-column prop="time" label="时间" width="110" sortable="custom">
                <template #default="{ row }">{{ row.time || "--:--:--" }}</template>
              </el-table-column>
              <el-table-column prop="phone" label="账号" width="150">
                <!-- 服务端已遮（唯一出口）；前端不再自遮，直接渲染下发值 -->
                <template #default="{ row }">{{ row.phone }}</template>
              </el-table-column>
              <el-table-column prop="status" label="状态" width="130" sortable="custom">
                <!-- 探针只有 failed/其它 两态：非 failed 一律「正常」，原始码保留在 title -->
                <template #default="{ row }">
                  <span
                    class="badge"
                    :class="`badge--${statusLabel('probe', row.status).tone}`"
                    :title="statusLabel('probe', row.status).raw"
                  >{{ statusLabel("probe", row.status).label }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="message" label="说明" min-width="280" show-overflow-tooltip>
                <template #default="{ row }">{{ eventMessage(row, false) }}</template>
              </el-table-column>
            </el-table>
          </div>
          <div v-if="probeRows.length" class="logs-event-cards" aria-label="探针记录">
            <article v-for="(row, i) in probeRows" :key="'probe-' + i" class="logs-event-card">
              <div class="logs-event-top">
                <span class="logs-event-time">{{ row.time || "--:--:--" }}</span>
                <span class="badge" :class="`badge--${statusLabel('probe', row.status).tone}`" :title="statusLabel('probe', row.status).raw">
                  {{ statusLabel("probe", row.status).label }}
                </span>
              </div>
              <div class="logs-event-phone">{{ row.phone }}</div>
              <p class="logs-event-msg">{{ eventMessage(row, false) }}</p>
            </article>
          </div>
          <p v-else class="logs-empty logs-event-empty">
            该日无探针记录
            <button
              v-if="payload?.recent_probe_date"
              type="button"
              class="btn btn--ghost btn--sm"
              @click="jumpToRecent(payload.recent_probe_date)"
            >
              查看 {{ payload.recent_probe_date }}
            </button>
          </p>
          <div v-if="(payload?.probe_events.length ?? 0) > 0" class="logs-pager">
            <span class="logs-pager-total">{{ eventCountText(payload?.probe_events.length ?? 0) }}</span>
            <el-pagination
              v-model:current-page="probeTable.page"
              v-model:page-size="probeTable.size"
              :total="payload?.probe_events.length ?? 0"
              :page-sizes="EVENT_PAGE_SIZES"
              layout="total, sizes, prev, pager, next"
              @size-change="probeTable.page = 1"
            />
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
/* 卡片与日志正文外观复用全局样式（.card 来自 adminator/app.css；.log-box/.log-file 等见
   app.css 的日志页段），此处只写本页专有排布，避免与全局规则叠加。 */
.logs-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.logs-title {
  margin: 0;
  font-size: var(--fs-title);
  font-weight: 700;
  color: var(--t-base);
}
.logs-file {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--t-muted);
}
.logs-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
/* 检索组（输入框 + 「检索」按钮）：同排显示，窄屏整组换行（.logs-tools 已 flex-wrap）。
   flex-basis 给这一组的目标宽度，min-width 保底——否则输入框会被同排按钮挤成细条。 */
.logs-search {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 1 320px;
  min-width: 232px;
}
.logs-search .input {
  flex: 1 1 auto;
  min-width: 0;
  /* 与同排按钮同高（--ctl-h-md=32）：全局 .input 是 40px，与 32px 的 .btn--sm 并排
     时检索组比工具按钮高 8px，二者顶缘差 4.2px → 网格判定为「控件与标签分行」
     （122 §R2.2-c：/data/logs 工具行与检索表单在 640–2954 全桌面档错位）。 */
  height: var(--ctl-h-md);
  min-height: var(--ctl-h-md);
}
.logs-info {
  font-size: var(--fs-label);
  color: var(--t-muted);
}
/* 签到日志卡头专有排布（.logs-head--log）。两件事：
   ① 状态文本独占一整行、落到控件行之下（flex-basis:100% 强制换行）；
   ② 控件组吃满剩余行宽并把控件右贴 —— 否则 .logs-tools 的 intrinsic 宽度偏小
      （实测 1366 档只算到 424.8px，而三个控件同排需要 494.8px），「导出日志」会被
      挤到第二行；控件组一换行，标题（align-items:center）就对不齐控件行了。
   只作用在本卡：运行巡检卡（共用 .logs-head / .logs-tools）宽度够用，不动它。 */
.logs-head--log .logs-tools {
  flex: 1 1 auto;
  min-width: 0;
  justify-content: flex-end;
}
.logs-head--log .logs-info {
  flex: 1 0 100%;
}
.logs-datebar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
  margin-bottom: 12px;
}
.logs-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
/* 日期选择器宽度：EP .el-date-editor 默认 220px，收到与工具行同档。 */
.logs-date {
  width: 152px;
}
.logs-label {
  font-size: 12px;
  color: var(--t-muted);
}
.logs-check,
.logs-level {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-label);
  color: var(--t-muted);
}
.logs-count,
.logs-pager-total {
  font-size: 12px;
  color: var(--t-muted);
}
/* 页签计数：原先是紧贴标题的裸数字（渲染成「签到事件2」）。给一个小胶囊，
   既分开标题与数字，也让三档页签的计数在同一基线上可扫读。 */
.logs-count {
  display: inline-block;
  min-width: 18px;
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--bg-muted);
  font-size: 11px;
  line-height: 18px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
/* 操作组：桌面与检索框同排；窄屏改为两列等宽按钮，不再右贴留出半行空白。 */
.logs-actions,
.logs-date-actions,
.logs-filters {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* 桌面不渲染移动卡片；窄屏媒体块里再显示。 */
.logs-event-cards,
.logs-event-empty,
.run-mobile-cards { display: none; }
@media (max-width: 720px) {
  .logs-head { align-items: stretch; gap: 10px; }
  .logs-tools {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 8px;
    width: 100%;
    margin-left: 0;
  }
  .logs-search { width: 100%; flex: 1 1 auto; min-width: 0; }
  .logs-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  .logs-actions > * { width: 100%; justify-content: center; }
  .logs-datebar {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    align-items: stretch;
    gap: 8px;
  }
  .logs-field { width: 100%; }
  /* EP 的 .el-date-editor.el-input 会把宽度锁在 220px；带祖先的 :deep 选择器
     才压得过它，让日期框与下方「查看该日日志」按钮同宽。 */
  .logs-field :deep(.logs-date) { width: 100%; }
  .logs-date-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  .logs-date-actions > * { width: 100%; justify-content: center; }
  .logs-date-actions > :only-child { grid-column: 1 / -1; }
  .logs-filters {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 6px;
  }
  .logs-level,
  .logs-check { min-height: 28px; }
  /* 内容少时不再留 220px 空白；长日志到 320px 后自身滚动。 */
  .log-box { height: auto; min-height: 140px; max-height: 320px; }
  /* 4 个页签在 390 下放不下，EP 会显示左右箭头并把第 4 个裁掉；
     收窄内距与字号让四个页签完整露出，保留日志页原有的滑动活动条。 */
  :deep(.el-tabs__item) { padding: 0 10px; font-size: 13px; }
  :deep(.el-tabs__header) { margin-bottom: 12px; }

  /* 事件/探针表 5 列在 390 只能看到前 3 列，尝试与说明被裁在视口外；
     窄屏改为逐条卡片，桌面仍用 el-table。 */
  .logs-table-wrap { display: none; }
  .logs-event-cards { display: grid; gap: 10px; }
  .logs-event-empty { display: block; }
  .logs-event-card {
    padding: 12px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--bg-card);
    min-width: 0;
  }
  .logs-event-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .logs-event-time {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--t-muted);
    font-variant-numeric: tabular-nums;
  }
  .logs-event-phone {
    margin-top: 6px;
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--t-base);
    overflow-wrap: anywhere;
  }
  .logs-event-attempt {
    margin-top: 2px;
    font-size: 12px;
    color: var(--t-muted);
  }
  .logs-event-msg {
    margin: 8px 0 0;
    padding-top: 8px;
    border-top: 1px solid var(--border-soft);
    font-size: 12px;
    line-height: 1.6;
    color: var(--t-sub);
    overflow-wrap: anywhere;
  }

  /* 运行巡检的四张 el-table 在 390 下同样只露前 3 列；窄屏换成卡片，
     保留执行体/心跳/吞吐、轮次摘要与时间线说明。 */
  .run-table-wrap { display: none; }
  .run-mobile-cards { display: grid; gap: 10px; }
  .run-mobile-card {
    display: block;
    padding: 12px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--bg-card);
    min-width: 0;
    font-size: 12px;
  }
  .run-mobile-card--button {
    width: 100%;
    text-align: left;
    cursor: pointer;
    color: inherit;
    font: inherit;
  }
  .run-mobile-card.is-current {
    border-color: var(--primary);
    background: var(--primary-soft);
  }
  .run-mobile-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .run-mobile-meta {
    display: block;
    margin-top: 6px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--t-sub);
  }
  .run-mobile-msg {
    margin: 8px 0 0;
    padding-top: 8px;
    border-top: 1px solid var(--border-soft);
    font-size: 12px;
    line-height: 1.6;
    color: var(--t-sub);
    overflow-wrap: anywhere;
  }
}
.logs-empty {
  padding: 12px 0;
  color: var(--t-muted);
  font-size: 13px;
}
/* 运行巡检块的空态与说明：刻意**不**复用 `.logs-empty` / `.logs-info` 类——同名类会让
   既有 e2e 选择器一次命中多处（Playwright 严格模式报错）。 */
.run-empty {
  padding: 12px 0;
  color: var(--t-muted);
  font-size: 13px;
}
.run-info,
.run-note {
  font-size: var(--fs-label);
  color: var(--t-muted);
}
/* 4K 长行收口（124 §A.6 实测运行巡检空态 <p>=3470px）。这两个类是本组件私有
   （见上一条注释），样式住本组件，不写全局 app.css——全局表写组件私有类会泄入
   本页且随组件改名失效。 */
.run-empty > p,
.run-note {
  max-width: var(--text-line-max);
}
.logs-retry {
  margin-bottom: 8px;
}
.logs-pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 12px;
}
/* 日志正文分页器（与事件表 .logs-pager 分开：一个绑服务端页，一个是本地表页）。 */
.logs-logpager {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  margin-top: 12px;
  max-width: 100%;
  overflow-x: auto;
}
@media (max-width: 560px) {
  .logs-logpager { justify-content: center; }
}
/* 运行巡检块：摘要与时间线同卡；两块表各占一行，窄屏由 el-table 自身横滚。 */
#run-panel {
  margin-bottom: 12px;
}
/* 运行进度（实时）块：一屏两带（全局六节点总数 + 每执行体一行 + 最近事件）。
   外观用全局令牌（--border / --bg-card / --r-inner），与同卡其他块一致；
   六节点总数用 flex 自适应（flex-basis 90px），360px 下折行、不横向溢出。 */
.run-live {
  border: 1px solid var(--border);
  border-radius: var(--r-inner, 10px);
  background: var(--bg-card);
  padding: 10px 12px;
  margin-bottom: 12px;
}
.run-live-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.run-live-status {
  font-size: var(--fs-label);
  font-weight: 600;
}
.run-live-status.is-on {
  color: var(--state-ok-fg);
}
.run-live-status.is-off {
  color: var(--t-muted);
}
.live-totals {
  display: flex;
  flex-wrap: wrap;
  gap: 8px var(--sp-2, 8px);
  margin-bottom: 4px;
}
.live-total {
  flex: 1 1 90px;
  min-width: 80px;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.live-total-label {
  font-size: 12px;
  color: var(--t-muted);
}
.live-total-value {
  /* 展示档字号走令牌（--fs-display=22px）：裸 px 必须落在六档或 ≥24，此法最稳 */
  font-size: var(--fs-display);
  font-weight: 700;
  color: var(--t-base);
  font-variant-numeric: tabular-nums;
}
.live-recent {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
.live-recent-label {
  font-size: 12px;
  color: var(--t-muted);
}
.live-recent-text {
  font-size: 12px;
  color: var(--t-base);
  font-family: var(--font-mono);
  word-break: break-all;
}
.run-sub {
  margin: 12px 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--t-base);
}
/* 执行体健康卡与审计链状态块：与「运行进度」块同族（同一张卡内的兄弟块），
   只加一条上分隔线，避免与卡内其它块粘连。 */
.run-block {
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.run-block .run-sub {
  margin-top: 0;
}
.exec-name {
  color: var(--t-base);
}
.exec-flag {
  margin-left: 6px;
  font-size: 11px;
  color: var(--badge-warn-fg);
  background: var(--badge-warn-bg);
  border-radius: var(--r-ctl);
  padding: 0 4px;
}
.exec-ident {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--t-muted);
}
.audit-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin: 0 0 6px;
}
/* 已知欠账：**中性**呈现，刻意不借任何状态色——它与「篡改」不同色是判据之一。 */
.audit-debt {
  margin: 0 0 6px;
  padding-left: 18px;
  list-style: disc;
}
.audit-debt > li {
  font-size: var(--fs-label);
  color: var(--t-muted);
  max-width: var(--text-line-max);
}
.run-current {
  font-size: 12px;
  color: var(--t-muted);
}
</style>
