<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { api, toast } from "../lib/shell";
import { isValidDate } from "./date-guard.js";
import {
  ALL_LOG_LEVEL,
  WARN_LOG_LEVEL,
  EVENT_PAGE_SIZES,
  TAB_KEYS,
  buildExportUrl,
  buildLogsQuery,
  eventCountText,
  eventMessage,
  infoText,
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
   · 翻页/排序是**客户端**行为（接口按日一次性返回当日结果集，封顶 5000 行）。

   日期语义（2026-10-03 修 blocking：原实现用服务端回显日期覆盖用户选择，导致
   查看该日/回到今天/空态跳转全部失效）：
   · `viewDate === ""` = **跟随最新**（服务端解析最近有日志的一天，通常是今天）；
   · 「查看该日」「空态跳转」pin 住某天并写进 `?date=`（可分享、刷新不丢）；pin 态**不轮询**；
   · 「回到今天」清空 = 回到跟随态。
   运行巡检块跟随同一个日期，故两块内容始终同一天。 */

const POLL_MS = 10000;
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
const showAll = ref(false);
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

let busy = false;
let runBusy = false;
/** 在途巡检请求期间到达的请求（合并：后到覆盖先到），不许静默丢弃 */
let runPending: { day: string; executor: string; keepSelection: boolean } | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
let firstLoad = true;

const info = computed(() => (payload.value ? infoText(payload.value) : ""));
const logText = computed(() => (payload.value ? payload.value.logs.join("\n") : ""));
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
    const resp = await api<LogsPayload>("GET", buildLogsQuery(date, keyword.value, showAll.value, levelOverride.value));
    payload.value = resp;
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

function toggleAll(): void {
  showAll.value = !showAll.value;
  void load();
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
    })
  ) {
    void load("poll");
  }
}

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
  timer = setInterval(tick, POLL_MS);
});

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer);
});
</script>

<template>
  <div class="logs">
    <el-tabs v-model="tab">
      <el-tab-pane label="日志" name="main">
        <!-- 运行巡检：巡检页首块。轮级摘要一眼看完，点某轮展开该轮时间线。 -->
        <div id="run-panel" class="card">
          <div class="logs-head">
            <h2 class="logs-title">运行巡检</h2>
            <div class="logs-tools">
              <button type="button" class="btn btn--ghost btn--sm" :disabled="runLoading" @click="refreshRuns">
                刷新进度
              </button>
              <span id="run-window" class="run-info">{{ runWindowLine }}</span>
            </div>
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
            <p v-if="runTruncatedLine" id="run-summary-truncated" class="run-note">
              {{ runTruncatedLine }}
            </p>
            <h3 class="run-sub">{{ timelineTitle }}</h3>
            <div id="run-timeline">
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
              <p v-if="runPayload?.events_truncated" id="run-timeline-truncated" class="run-note">
                时间线已截断（本次最多 {{ runPayload.events_limit }} 行）。
              </p>
            </div>
          </template>
        </div>

        <div class="card">
          <div class="logs-head">
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
              <button
                type="button"
                class="btn btn--ghost btn--sm"
                :aria-pressed="showAll ? 'true' : 'false'"
                @click="toggleAll"
              >
                {{ showAll ? "回到最近日志" : "显示全部日志" }}
              </button>
              <a v-if="canExport" class="btn btn--ghost btn--sm" :href="exportHref" :download="exportName" role="button">导出日志</a>
              <button v-else type="button" class="btn btn--ghost btn--sm" disabled title="尚未加载出可导出的日期">导出日志</button>
              <span class="logs-info" role="status">{{ loading ? "正在加载日志…" : info }}</span>
            </div>
          </div>

          <div class="logs-datebar">
            <label class="logs-field">
              <span class="logs-label">查看日期</span>
              <input v-model="dateInput" class="input" type="date" />
            </label>
            <button type="button" class="btn btn--ghost btn--sm" :disabled="loading" @click="viewDateAction">查看该日日志</button>
            <button v-if="payload && !isToday" type="button" class="btn btn--ghost btn--sm" @click="backToToday">回到今天</button>
            <!-- 级别档开关：默认收起 INFO（巡检只看 WARN／ERROR）。类名刻意与
                 `.logs-check`（自动刷新）分开——两处同用一个类会让既有 e2e 选择器命中两个。 -->
            <label class="logs-level">
              <input id="logs-level-warn" v-model="warnOnly" type="checkbox" @change="onLevelChange" />
              <span>只看告警（收起 INFO）</span>
            </label>
            <label class="logs-check">
              <input v-model="autoRefresh" type="checkbox" @change="onAutoRefreshChange" />
              <span>自动刷新（10 秒，仅跟随最新时）</span>
            </label>
          </div>

          <el-alert v-if="errorText" type="error" :title="errorText" :closable="false" show-icon style="margin: 8px 0" />
          <div v-if="errorText" class="logs-retry">
            <button type="button" class="btn btn--ghost btn--sm" :disabled="loading" @click="load()">重试</button>
          </div>
          <pre v-show="logText" ref="logBox" class="log-box" role="log" aria-live="off">{{ logText }}</pre>
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

      <el-tab-pane name="signev">
        <template #label>签到事件 <span class="logs-count">{{ payload?.sign_events.length ?? 0 }}</span></template>
        <div class="card">
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
  gap: 10px;
  margin-bottom: 10px;
}
.logs-title {
  margin: 0;
  font-size: 15px;
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
}
.logs-info {
  font-size: 12.5px;
  color: var(--t-muted);
}
.logs-datebar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 10px;
  margin-bottom: 10px;
}
.logs-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
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
  font-size: 12.5px;
  color: var(--t-muted);
}
.logs-count,
.logs-pager-total {
  font-size: 12px;
  color: var(--t-muted);
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
  font-size: 12.5px;
  color: var(--t-muted);
}
.logs-retry {
  margin-bottom: 8px;
}
.logs-pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 10px;
}
/* 运行巡检块：摘要与时间线同卡；两块表各占一行，窄屏由 el-table 自身横滚。 */
#run-panel {
  margin-bottom: 12px;
}
.run-sub {
  margin: 12px 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--t-base);
}
.run-current {
  font-size: 12px;
  color: var(--t-muted);
}
</style>
