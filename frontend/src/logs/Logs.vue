<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { api, maskPhone, toast } from "../lib/shell";
import { isValidDate } from "./date-guard.js";
import {
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

/* 签到日志页（GET /api/logs，三分区：日志 / 签到事件 / 探针记录）。
   纪律：
   · 脱敏单出口在服务端（日志行 _mask_log_phones、事件 phone _mask_phone + 截断）；前端对
     已脱敏值再走一遍 shell.maskPhone 是**防御性**的（与 legacy 同做法）；全程零 v-html。
   · 三元组 total_lines/returned/truncated 由服务端同轴推出，前端只按 truncated 显示
     「已截断」，不得用行数自行推断（见 format.ts）。
   · 翻页/排序是**客户端**行为（接口按日一次性返回当日结果集，封顶 5000 行）。

   日期语义（2026-10-03 修 blocking：原实现用服务端回显日期覆盖用户选择，导致
   查看该日/回到今天/空态跳转全部失效）：
   · `viewDate === ""` = **跟随最新**（服务端解析最近有日志的一天，通常是今天）；
   · 「查看该日」「空态跳转」pin 住某天并写进 `?date=`（可分享、刷新不丢）；pin 态**不轮询**；
   · 「回到今天」清空 = 回到跟随态。 */

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
const autoRefresh = ref(true);
const logBox = ref<PreElement | null>(null);
const signTable = ref<TableState>({ page: 1, size: 20, prop: "time", order: "descending" });
const probeTable = ref<TableState>({ page: 1, size: 20, prop: "time", order: "descending" });

let busy = false;
let timer: ReturnType<typeof setInterval> | null = null;
let firstLoad = true;

const info = computed(() => (payload.value ? infoText(payload.value) : ""));
const logText = computed(() => (payload.value ? payload.value.logs.join("\n") : ""));
const following = computed(() => viewDate.value === "");
const isToday = computed(() => payload.value?.is_today === true);
const canExport = computed(() => !!loadedDate.value);
const exportHref = computed(() => (loadedDate.value ? buildExportUrl(loadedDate.value) : ""));
const exportName = computed(() => payload.value?.log_file ?? "sign.log");

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
  // 导航请求用用户 pin 的日期（可能为空 = 跟随最新）；轮询刷新沿用**已加载的那一天**，
  // 否则跟随态下服务端会重新解析日期、把用户正在看的内容换掉。
  const date = mode === "poll" ? loadedDate.value : viewDate.value;
  try {
    const resp = await api<LogsPayload>("GET", buildLogsQuery(date, keyword.value, showAll.value));
    payload.value = resp;
    loadedDate.value = resp.date;
    dateInput.value = resp.date;
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
}

function search(): void {
  keyword.value = searchInput.value.trim();
  void load();
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
        <div class="card">
          <div class="logs-head">
            <h2 class="logs-title">签到日志 <span class="logs-file">{{ payload?.log_file ?? "" }}</span></h2>
            <div class="logs-tools">
              <input v-model="searchInput" class="input" type="search" placeholder="检索日志关键字" @keyup.enter="search" />
              <button type="button" class="btn btn--ghost btn--sm" :disabled="loading" @click="search">检索</button>
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
          <div v-if="!logText && !loading && !errorText" class="logs-empty">
            <p>
              {{ keyword ? "（无匹配日志行）" : viewDate ? `（${viewDate} 无签到日志）` : "（暂无签到日志，等待定时任务执行…）" }}
              <button
                v-if="!keyword && payload?.recent_log_date"
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
              <!-- 服务端已脱敏；这里再走一遍 shell.maskPhone 是防御性的（与 legacy 同做法） -->
              <template #default="{ row }">{{ maskPhone(row.phone) }}</template>
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
              <template #default="{ row }">{{ maskPhone(row.phone) }}</template>
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
.logs-check {
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
</style>
