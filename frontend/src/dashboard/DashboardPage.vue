<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { api, getServerNow, shellBase, swapMs } from "../lib/shell";
import KpiCard from "./KpiCard.vue";
import {
  DEFAULT_CAL_NOTE,
  SIGN_DAYS,
  SIGN_EVENTS_PATH,
  WEEK,
  acceptAccounts,
  announcementView,
  calendarView,
  capacityRows,
  clockView,
  distView,
  emptyState,
  fmtMonth,
  normalizeDaily,
  num,
  pauseView,
  pendingKpiView,
  pingView,
  rateKpiView,
  slotsView,
  todayAccountKpiViews,
  trendView,
} from "./model.js";

/* 数据看板（管理端首页 /data/dashboard）。

   分工：口径（双口径聚合、各视图的数据变换、词表与文案）全在 `model.js`（纯 JS，被
   Python 守卫抽真函数跑 / Vitest 覆盖）；本组件只做**编排与绘制**——请求、Chart.js 实例
   生命周期、热力图换月动效、页级状态条。

   ## 与 legacy（`pages/data_dashboard.js`）的关系
   · 六个数据点分属五次请求（settings / sign-events / time-prefs / accounts / clock+announcement），
     单点失败只降级对应卡片，页级状态条汇总失败数并提供统一重试；
   · Chart.js 仍是仓库内置的 vendor UMD（`web/static/vendor/chartjs/chart.min.js`，v4.5.1
     按需构建）：模板以 defer 先行加载、本模块执行时 `window.Chart` 已就位。数据刷新优先
     `chart.update()`，主题切换才重建（色值取自 CSS 变量，换肤后须重新取色）。
   · 手机号脱敏是服务端单出口；本页只插值渲染文本，零 v-html。
   · 主题切换监听 `yiban:theme`（document 级，core.js 派发）：重建趋势 / 分布 / 时间片三图。 */

const props = withDefaults(defineProps<{ calNote?: string }>(), { calNote: "" });

const BASE = shellBase();
const CAL_NOTE_BASE = props.calNote || DEFAULT_CAL_NOTE;

/* ---------------- 状态 ---------------- */
const state = reactive(emptyState());
const settingsState = ref<"loading" | "ok" | "fail">("loading");
const settingsData = ref<any>(null);
const pendingState = ref<"loading" | "ok" | "fail">("loading");
const pendingData = ref<any>(null);
const clockState = ref<"loading" | "ok" | "fail">("loading");
const clock = ref<any>(null);
const annState = ref<"loading" | "ok" | "fail">("loading");
const announcement = ref<any>(null);
const pingState = ref<null | "loading" | { ok: boolean; text: string; detail: string }>(null);

const statusTone = ref<"info" | "danger">("danger");
const statusText = ref("");
const statusVisible = ref(false);
const statusRetryable = ref(false);

const calMonth = ref<string | null>(null);
const calNote = ref(CAL_NOTE_BASE);
const calGrid = ref<HTMLElement | null>(null);
const calLabel = ref<HTMLElement | null>(null);

const trendCanvas = ref<HTMLCanvasElement | null>(null);
const distCanvas = ref<HTMLCanvasElement | null>(null);
const slotsCanvas = ref<HTMLCanvasElement | null>(null);
/** 时间片加载成功标志：onTheme 的重绘门（失败时置 false，防陈旧数据洗掉错误态）。 */
const slotsLoaded = ref(false);
const overlays = reactive<Record<string, string>>({ trend: "loading", dist: "loading", slots: "loading" });
const overlayMsgs = reactive<Record<string, string>>({ trend: "", dist: "", slots: "" });
const cov = reactive({ trend: "", dist: "", slots: "" });
const trendMeta = ref<[string, string][]>([]);
const distMeta = ref<[string, string][]>([]);
const slotsMeta = ref<[string, string][]>([]);

/* ---------------- 视图（读口径层） ---------------- */
// 今日成功/失败账号：账号口径，与容量卡脱钩（原先此处放活跃账号/注册用户，与容量卡同源同数）。
const todayKpis = computed(() => todayAccountKpiViews(state, getServerNow()));
const capRows = computed(() => (settingsState.value === "ok" ? capacityRows(settingsData.value) : null));
const pause = computed(() => (settingsState.value === "ok" ? pauseView(settingsData.value) : null));
const pendingKpi = computed(() =>
  pendingState.value === "loading" ? null : pendingKpiView(pendingState.value === "ok" ? pendingData.value : null),
);
const rateKpi = computed(() => rateKpiView(state, getServerNow()));
const calData = computed(() => calendarView(state, { month: calMonth.value, now: getServerNow() }));

/* ---------------- 图表宿主 ---------------- */
interface ChartLike {
  canvas?: unknown;
  data: any;
  options: any;
  update(): void;
  destroy(): void;
}
const charts = new Map<string, ChartLike>();

function chartCtor(): any {
  return (window as any).Chart;
}

function token(name: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (v || "").trim();
}
function palette(): Record<string, string> {
  return {
    primary: token("--primary"), success: token("--success"), danger: token("--danger"),
    warning: token("--warning"), info: token("--info"), purple: token("--purple"),
    teal: token("--teal"), text: token("--t-base"), muted: token("--t-muted"),
    light: token("--t-light"), soft: token("--border-soft"), border: token("--border"),
    card: token("--bg-card"),
  };
}

const REDUCED = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

function chartDefaults(t: Record<string, string>): void {
  const C = chartCtor();
  if (!C) return;
  C.defaults.font.family = "'Inter','Noto Sans SC',system-ui,sans-serif";
  C.defaults.font.size = 12;
  C.defaults.color = t.muted;
  C.defaults.borderColor = t.soft;
}
function baseOpts(t: Record<string, string>): any {
  return {
    responsive: true, maintainAspectRatio: false,
    animation: REDUCED ? false : { duration: 400 },
    plugins: {
      legend: {
        position: "bottom",
        labels: { color: t.muted, usePointStyle: true, boxWidth: 8, boxHeight: 8, padding: 16 },
      },
      tooltip: {
        backgroundColor: t.text, titleColor: t.card, bodyColor: t.card,
        padding: 10, cornerRadius: 6, displayColors: true,
      },
    },
  };
}

/** 绘制 / 更新一张图：同 canvas 的已有实例优先 `update()`（数据刷新），否则新建。
 *  容器隐藏（无 client rects）时不建实例；`window.Chart` 缺失时落「图表库未加载」。 */
function renderChart(key: string, canvas: HTMLCanvasElement | null, config: any): boolean {
  const C = chartCtor();
  if (!C) {
    setOverlay(key, "error", "图表库未加载");
    return false;
  }
  const existing = charts.get(key);
  if (existing && existing.canvas === canvas) {
    chartDefaults(palette());
    existing.data = config.data;
    existing.options = config.options;
    existing.update();
    return true;
  }
  if (existing) {
    try { existing.destroy(); } catch (e) { /* 忽略 */ }
    charts.delete(key);
  }
  if (!canvas || !canvas.getClientRects().length) return false;
  try {
    chartDefaults(palette());
    charts.set(key, new C(canvas, config) as ChartLike);
    return true;
  } catch (e) {
    setOverlay(key, "error", "图表渲染失败");
    return false;
  }
}
function destroyChart(key: string): void {
  const c = charts.get(key);
  if (c) {
    try { c.destroy(); } catch (e) { /* 忽略 */ }
    charts.delete(key);
  }
}
function destroyAll(): void {
  for (const key of Array.from(charts.keys())) destroyChart(key);
}
function setOverlay(key: string, mode: string, msg?: string): void {
  overlays[key] = mode;
  overlayMsgs[key] = msg || "";
}

function paintTrend(): void {
  const v = trendView(state);
  if (!v) {
    destroyChart("trend");
    setOverlay("trend", "empty", "最近 30 天无真实签到记录");
    cov.trend = "无数据";
    trendMeta.value = [];
    return;
  }
  const t = palette();
  const opt = baseOpts(t);
  opt.interaction = { mode: "index", intersect: false };
  opt.scales = {
    x: { stacked: true, ticks: { color: t.muted, autoSkip: true, maxRotation: 0, maxTicksLimit: 12 }, grid: { display: false }, border: { color: t.soft } },
    y: { stacked: true, beginAtZero: true, ticks: { color: t.muted, precision: 0, callback: (x: any) => num(x) }, grid: { color: t.soft }, border: { display: false } },
  };
  opt.plugins.tooltip.callbacks = { label: (c: any) => c.dataset.label + "：" + num(c.parsed.y) + " 次" };
  opt.plugins.tooltip.mode = "index";
  opt.plugins.tooltip.intersect = false;
  const datasets = v.datasets.map((ds: any) => ({
    label: ds.label, data: ds.data, backgroundColor: t[ds.token] || t.light,
    stack: "sign", borderRadius: 3, barPercentage: 0.72,
  }));
  const drawn = renderChart("trend", trendCanvas.value, { type: "bar", data: { labels: v.labels, datasets }, options: opt });
  setOverlay("trend", drawn ? "none" : "error", "图表渲染失败");
  trendMeta.value = v.meta;
}

function paintDist(): void {
  const v = distView(state);
  if (!v) {
    destroyChart("dist");
    setOverlay("dist", "empty", "暂无签到结果数据");
    distMeta.value = [];
    return;
  }
  const t = palette();
  const opt = baseOpts(t);
  opt.cutout = "68%";
  opt.plugins.tooltip.callbacks = {
    // 占比分母从**当前**图数据现算，不闭包捕获——数据刷新走 update() 时旧闭包会算错
    label: (c: any) => {
      const total = c.dataset.data.reduce((n: number, x: any) => n + (Number(x) || 0), 0);
      const val = Number(c.parsed) || 0;
      return c.label + "：" + num(val) + " 账号（" + (total > 0 ? Math.round(val / total * 1000) / 10 : 0) + "%）";
    },
  };
  const drawn = renderChart("dist", distCanvas.value, {
    type: "doughnut",
    data: {
      labels: v.labels,
      datasets: [{
        data: v.data,
        backgroundColor: v.tokens.map((k: string) => t[k] || t.light),
        borderColor: t.card, borderWidth: 2,
      }],
    },
    options: opt,
  });
  setOverlay("dist", drawn ? "none" : "error", "图表渲染失败");
  distMeta.value = v.meta;
}

function paintSlots(): void {
  const v = slotsView(state.slots);
  if (!v) {
    destroyChart("slots");
    setOverlay("slots", "empty", "当前无可用的自选时间片");
    cov.slots = "无数据";
    slotsMeta.value = [];
    return;
  }
  const t = palette();
  const opt = baseOpts(t);
  opt.indexAxis = "y";
  opt.scales = {
    x: { beginAtZero: true, ticks: { color: t.muted, precision: 0, callback: (x: any) => num(x) }, grid: { color: t.soft }, border: { display: false } },
    y: { ticks: { color: t.muted }, grid: { display: false }, border: { color: t.soft } },
  };
  opt.plugins.tooltip.callbacks = { label: (c: any) => c.dataset.label + "：" + num(c.parsed.x) + " 人" };
  const drawn = renderChart("slots", slotsCanvas.value, {
    type: "bar",
    data: {
      labels: v.labels,
      datasets: [
        { label: "自选人数", data: v.counts, backgroundColor: t.primary, borderRadius: 4, barPercentage: 0.72 },
        { label: "该时段人数上限", data: v.caps, backgroundColor: t.soft, borderRadius: 4, barPercentage: 0.72 },
      ],
    },
    options: opt,
  });
  setOverlay("slots", drawn ? "none" : "error", "图表渲染失败");
  cov.slots = v.coverage;
  slotsMeta.value = v.meta;
}

/* ---------------- 请求 ---------------- */
async function loadSettings(): Promise<boolean> {
  try {
    const d = await api<any>("GET", "/api/settings");
    settingsData.value = d;
    settingsState.value = "ok";
    applyWeekend(d);
    return true;
  } catch (e) {
    settingsData.value = null;
    settingsState.value = "fail";
    return false;
  }
}

function applyWeekend(d: any): void {
  // 周末开关决定「今日无结果」的解释：设置到达后重算成功率文案
  state.satSign = Number(d?.saturday_sign) === 1;
  state.sunSign = Number(d?.sunday_sign) === 1;
  state.weekendKnown = true;
}

async function loadSign(): Promise<boolean> {
  try {
    const d = await api<any>("GET", SIGN_EVENTS_PATH);
    const norm = normalizeDaily((d && d.daily_stats) || []);
    state.dailyMap = norm.map;
    state.dailyDays = norm.days;
    const acc = acceptAccounts(d && d.accounts_stats);
    state.accountsTotal = acc.total;
    state.statusAccounts = acc.byStatus;
    state.dayFinal = acc.dayFinal;
    state.signLoaded = true;
    state.signFailed = false;
    const days = Number((d && d.days) || SIGN_DAYS);
    cov.trend = "最近 " + days + " 天 · 仅真实签到";
    cov.dist = cov.trend;
    calNote.value = CAL_NOTE_BASE;
    paintTrend();
    paintDist();
    return true;
  } catch (e) {
    // 失败时**清空**签到数据（不只置 signFailed）：否则此前成功过的 dailyMap/dayFinal 会在
    // 任意 `yiban:theme` 事件里被 onTheme 用陈旧数据重绘、把错误 overlay 洗回正常态。
    // signLoaded 一并置 false，作为 onTheme 的重绘门（signFailed 只护成功率卡）。
    state.signLoaded = false;
    state.signFailed = true;
    state.dailyMap = {};
    state.dailyDays = [];
    state.accountsTotal = 0;
    state.statusAccounts = {};
    state.dayFinal = {};
    const msg = (e as Error)?.message || "请求失败";
    destroyChart("trend");
    destroyChart("dist");
    setOverlay("trend", "error", "签到事件加载失败：" + msg);
    setOverlay("dist", "error", "签到事件加载失败：" + msg);
    trendMeta.value = [];
    distMeta.value = [];
    calNote.value = "签到事件加载失败：" + msg;
    return false;
  }
}

async function loadSlots(): Promise<boolean> {
  try {
    const d = await api<any>("GET", "/api/time-prefs/stats");
    state.slots = ((d && d.slots) || []).filter((s: any) => s && !s.disabled);
    slotsLoaded.value = true;
    paintSlots();
    return true;
  } catch (e) {
    // 失败清空 state.slots 并撤下成功标志：否则重试失败后，任意 yiban:theme 会命中
    // onTheme 的 `slotsLoaded && state.slots.length` 门控……不清空则旧数据重绘洗掉错误态。
    state.slots = [];
    slotsLoaded.value = false;
    destroyChart("slots");
    setOverlay("slots", "error", "时间片数据加载失败：" + ((e as Error)?.message || "请求失败"));
    slotsMeta.value = [];
    return false;
  }
}

async function loadPending(): Promise<boolean> {
  try {
    pendingData.value = await api<any>("GET", "/api/accounts");
    pendingState.value = "ok";
    return true;
  } catch (e) {
    pendingData.value = null;
    pendingState.value = "fail";
    return false;
  }
}

async function loadHealth(): Promise<boolean> {
  clockState.value = "loading";
  annState.value = "loading";
  // 两个请求**并行**（与 legacy 的 Promise.all 同形）：时钟与公告互不依赖，串行 await 会把
  // 首屏多拖一个 RTT。各自 settle 后由 loadAll 按"是否全成功"收放页级状态条——等价于
  // legacy 里任一失败即让 loadHealth 的 Promise.all 拒绝。
  const clockTask = (async (): Promise<boolean> => {
    try {
      const d = await api<any>("GET", "/api/clock");
      clock.value = clockView(d, Date.now());
      clockState.value = clock.value ? "ok" : "fail";
      return clockState.value === "ok";
    } catch (e) {
      clock.value = null;
      clockState.value = "fail";
      return false;
    }
  })();
  const annTask = (async (): Promise<boolean> => {
    try {
      const d = await api<any>("GET", "/api/announcement");
      announcement.value = announcementView(d);
      annState.value = "ok";
      return true;
    } catch (e) {
      announcement.value = null;
      annState.value = "fail";
      return false;
    }
  })();
  const [clockOk, annOk] = await Promise.all([clockTask, annTask]);
  return clockOk && annOk;
}

async function doPing(): Promise<void> {
  pingState.value = "loading";
  try {
    const d = await api<any>("POST", "/api/ping");
    pingState.value = pingView(d);
  } catch (e) {
    pingState.value = { ok: false, text: "检测失败", detail: (e as Error)?.message || "请求失败" };
  }
}

async function loadAll(): Promise<void> {
  const rs = await Promise.all([loadSettings(), loadSign(), loadSlots(), loadPending(), loadHealth()]);
  const fails = rs.filter((ok) => !ok).length;
  if (fails > 0) {
    statusTone.value = "danger";
    statusText.value = "部分数据加载失败（" + fails + " 项），卡片内已标注";
    statusRetryable.value = true;
    statusVisible.value = true;
  } else {
    statusVisible.value = false;
  }
}

function retryAll(): void {
  statusTone.value = "info";
  statusText.value = "正在重新加载数据…";
  statusRetryable.value = false;
  statusVisible.value = true;
  void loadAll();
}

/* ---------------- 热力图换月动效（与日历页 calendar.js 的 shiftMonth 同口径） ---------------- */
const CAL_SWAP_MS = swapMs();
let calShiftSeq = 0;
let calShiftTimer: ReturnType<typeof setTimeout> | null = null;
function reduceMotion(): boolean {
  return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}
function clearCalShift(node: HTMLElement | null): void {
  if (node) node.classList.remove("is-swapping", "is-shifting-in", "is-shift-next", "is-shift-prev");
}
/** 取消在途换月：自增序号让所有已排队/在跑的 enter() 短路，并清掉兜底定时器。 */
function cancelCalShift(): void {
  calShiftSeq++;
  if (calShiftTimer != null) {
    clearTimeout(calShiftTimer);
    calShiftTimer = null;
  }
}
function shiftMonth(delta: number, instant: boolean): void {
  const now = getServerNow();
  const base = calMonth.value ? new Date(calMonth.value + "-01T00:00:00") : new Date(now.getFullYear(), now.getMonth(), 1);
  base.setMonth(base.getMonth() + delta);
  const next = fmtMonth(base);
  const grid = calGrid.value;
  const label = calLabel.value;
  // **同步**推进月份状态（legacy 同款）：动效只管视觉。若把赋值推迟到 enter()，160ms 内
  // 连点两次会都从旧月份算 next、净只进一格；且键盘即时分支会被在途 enter() 用旧目标覆盖。
  calMonth.value = next;
  // 所有分支（含即时 / reduceMotion / !delta）都取消在途切换：否则在途 enter() 的 seq
  // 守卫因未自增而仍成立，会把月份改回旧目标（键盘/即时操作被静默丢弃）。
  cancelCalShift();
  if (!delta || instant || reduceMotion() || !grid) {
    clearCalShift(grid);
    clearCalShift(label);
    return;
  }
  const seq = calShiftSeq;
  clearCalShift(grid);
  clearCalShift(label);
  grid.classList.add(delta > 0 ? "is-shift-next" : "is-shift-prev");
  grid.classList.add("is-swapping");
  if (label) label.classList.add("is-swapping");
  let done = false;
  const onEnd = (e: TransitionEvent) => { if (e.target === grid) void enter(); };
  const enter = async () => {
    if (done) return;
    done = true;
    grid.removeEventListener("transitionend", onEnd);
    if (calShiftTimer != null) {
      clearTimeout(calShiftTimer);
      calShiftTimer = null;
    }
    if (seq !== calShiftSeq) return;       // 已被下一次切换取代
    // 内容已在 shiftMonth 里同步换好（Vue 在 is-swapping 的 opacity 0 期间完成重渲染），
    // 这里只做视觉收尾，**不再**写 calMonth。
    await nextTick();
    if (seq !== calShiftSeq) return;
    grid.classList.remove("is-swapping");
    if (label) label.classList.remove("is-swapping");
    grid.classList.add("is-shifting-in");  // 进入起点：偏移到「来向」一侧（无过渡落位）
    if (label) label.classList.add("is-shifting-in");
    void grid.offsetWidth;                 // 提交起点，使摘类时产生进入过渡
    requestAnimationFrame(() => {
      if (seq !== calShiftSeq) return;
      grid.classList.remove("is-shifting-in", "is-shift-next", "is-shift-prev");
      if (label) label.classList.remove("is-shifting-in");
    });
  };
  grid.addEventListener("transitionend", onEnd);
  calShiftTimer = setTimeout(() => void enter(), CAL_SWAP_MS);
}

/* ---------------- 主题切换：重建读取 CSS 变量的图表 ---------------- */
function onTheme(): void {
  destroyAll();
  // 门控用「加载成功」标志而非仅看数据长度：失败路径已清空 state，但显式门更抗将来改动
  // （signLoaded 在 loadSign 失败时置 false；slotsLoaded 在 loadSlots 失败时置 false）。
  if (state.signLoaded && state.dailyDays.length) { paintTrend(); paintDist(); }
  if (slotsLoaded.value && state.slots.length) paintSlots();
}

onMounted(() => {
  calMonth.value = fmtMonth(getServerNow());
  document.addEventListener("yiban:theme", onTheme);
  void loadAll();
});
onBeforeUnmount(() => {
  cancelCalShift();                        // 清兜底定时器 + 让在途 enter() 短路
  document.removeEventListener("yiban:theme", onTheme);
  destroyAll();
});
</script>

<template>
  <div class="dash-page" id="dashboard-root">
    <div class="page-head">
      <h1 class="page-title">数据总览</h1>
      <p class="page-sub">账号、用户与签到结果的一屏汇总；待处理账号与异常排查入口就在对应卡片上。</p>
    </div>

    <p class="alert dash-status" id="dash-status" role="status" :class="statusTone" :hidden="!statusVisible">
      <span class="ico"><svg aria-hidden="true"><use href="#i-triangle-alert" /></svg></span>
      <span class="body" id="dash-status-text">{{ statusText }}</span>
      <button type="button" class="btn btn--ghost btn--sm" id="dash-retry-btn" :hidden="!statusRetryable" @click="retryAll">重试</button>
    </p>

    <section class="kpi-grid" aria-label="关键指标">
      <KpiCard k="accounts" color="success" icon="circle-check" label="今日成功账号" :href="BASE + '/work/accounts?tab=active'" :view="todayKpis && todayKpis.success" />
      <KpiCard k="users" color="danger" icon="circle-x" label="今日失败账号" :href="BASE + '/work/accounts?tab=active'" :view="todayKpis && todayKpis.fail" />
      <KpiCard k="rate" color="success" icon="circle-check" label="今日成功率" :href="BASE + '/data/logs'" :view="rateKpi" />
      <KpiCard k="pending" color="danger" icon="triangle-alert" label="待处理账号" :href="BASE + '/work/accounts?tab=pending'" :view="pendingKpi" :show-pill="false" />
    </section>

    <div class="grid">
      <!-- 每日签到结果（趋势） -->
      <section class="col-12 card">
        <div class="panel-head">
          <div class="panel-head-row">
            <h2 class="panel-title">每日签到结果</h2>
            <span class="card-action" id="trend-coverage">{{ cov.trend }}</span>
          </div>
        </div>
        <div class="chart-canvas-wrap chart-canvas-wrap--h300">
          <canvas ref="trendCanvas" id="chart-trend" role="img" aria-label="每日签到结果"></canvas>
          <div class="dash-chart-overlay" data-overlay="trend" :hidden="overlays.trend === 'none'">
            <span v-if="overlays.trend === 'loading'" class="spinner" aria-hidden="true"></span>
            <div v-else-if="overlays.trend === 'empty'" class="empty">
              <span class="empty__icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 3v18h18" /><path d="M18 17V9" /><path d="M13 17V5" /><path d="M8 17v-3" /></svg></span>
              <div class="empty__msg">{{ overlayMsgs.trend }}</div>
            </div>
            <div v-else class="dash-error-text">{{ overlayMsgs.trend }}</div>
          </div>
        </div>
        <div class="chart-meta-row" id="trend-meta" :hidden="!trendMeta.length">
          <div v-for="(pair, i) in trendMeta" :key="i" class="chart-meta-cell">
            <span class="chart-meta-label">{{ pair[0] }}</span>
            <span class="chart-meta-value">{{ pair[1] }}</span>
          </div>
        </div>
      </section>

      <!-- 签到账号分布 -->
      <section class="col-6 card">
        <div class="panel-head">
          <div class="panel-head-row">
            <h2 class="panel-title">签到账号分布</h2>
            <span class="card-action" id="dist-coverage">{{ cov.dist }}</span>
          </div>
        </div>
        <div class="chart-canvas-wrap chart-canvas-wrap--h280">
          <canvas ref="distCanvas" id="chart-dist" role="img" aria-label="签到账号分布"></canvas>
          <div class="dash-chart-overlay" data-overlay="dist" :hidden="overlays.dist === 'none'">
            <span v-if="overlays.dist === 'loading'" class="spinner" aria-hidden="true"></span>
            <div v-else-if="overlays.dist === 'empty'" class="empty">
              <span class="empty__icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 3v18h18" /><path d="M18 17V9" /><path d="M13 17V5" /><path d="M8 17v-3" /></svg></span>
              <div class="empty__msg">{{ overlayMsgs.dist }}</div>
            </div>
            <div v-else class="dash-error-text">{{ overlayMsgs.dist }}</div>
          </div>
        </div>
        <div class="chart-meta-row" id="dist-meta" :hidden="!distMeta.length">
          <div v-for="(pair, i) in distMeta" :key="i" class="chart-meta-cell">
            <span class="chart-meta-label">{{ pair[0] }}</span>
            <span class="chart-meta-value">{{ pair[1] }}</span>
          </div>
        </div>
      </section>

      <!-- 各时段自选人数 -->
      <section class="col-6 card">
        <div class="panel-head">
          <div class="panel-head-row">
            <h2 class="panel-title">各时段自选人数</h2>
            <span class="card-action" id="slots-coverage">{{ cov.slots }}</span>
          </div>
        </div>
        <div class="chart-canvas-wrap chart-canvas-wrap--h280">
          <canvas ref="slotsCanvas" id="chart-slots" role="img" aria-label="自选时间片已选人数"></canvas>
          <div class="dash-chart-overlay" data-overlay="slots" :hidden="overlays.slots === 'none'">
            <span v-if="overlays.slots === 'loading'" class="spinner" aria-hidden="true"></span>
            <div v-else-if="overlays.slots === 'empty'" class="empty">
              <span class="empty__icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 3v18h18" /><path d="M18 17V9" /><path d="M13 17V5" /><path d="M8 17v-3" /></svg></span>
              <div class="empty__msg">{{ overlayMsgs.slots }}</div>
            </div>
            <div v-else class="dash-error-text">{{ overlayMsgs.slots }}</div>
          </div>
        </div>
        <div class="chart-meta-row" id="slots-meta" :hidden="!slotsMeta.length">
          <div v-for="(pair, i) in slotsMeta" :key="i" class="chart-meta-cell">
            <span class="chart-meta-label">{{ pair[0] }}</span>
            <span class="chart-meta-value">{{ pair[1] }}</span>
          </div>
        </div>
      </section>

      <!-- 签到热力图 -->
      <section class="col-6 card">
        <div class="panel-head">
          <div class="panel-head-row">
            <h2 class="panel-title">签到热力图</h2>
            <div class="btn-group" role="group" aria-label="切换月份">
              <button type="button" class="btn btn--ghost btn--sm" id="cal-prev" aria-label="上一月" @click="shiftMonth(-1, $event.detail === 0)"><svg aria-hidden="true"><use href="#i-chevron-left" /></svg></button>
              <span class="btn btn--ghost btn--sm is-active" id="cal-label" ref="calLabel" aria-live="polite">{{ calData.label }}</span>
              <button type="button" class="btn btn--ghost btn--sm" id="cal-next" aria-label="下一月" @click="shiftMonth(1, $event.detail === 0)"><svg aria-hidden="true"><use href="#i-chevron-right" /></svg></button>
            </div>
          </div>
        </div>
        <div class="dash-cal-scroll">
          <div class="mini-cal-grid" id="mini-cal" ref="calGrid" role="grid" aria-label="签到结果日历">
            <div v-for="wd in WEEK" :key="'wd' + wd" class="mini-cal-wd">{{ wd }}</div>
            <template v-for="(c, i) in calData.cells" :key="i">
              <div v-if="c.other" class="mini-cal-day is-other" aria-hidden="true"></div>
              <div v-else :class="c.cls" :title="c.title" role="gridcell">{{ c.d }}</div>
            </template>
          </div>
        </div>
        <div class="dash-cal-legend" id="cal-legend">
          <span class="dash-cal-key"><i class="is-ok"></i>全部成功</span>
          <span class="dash-cal-key"><i class="is-fail"></i>有失败</span>
          <span class="dash-cal-key"><i class="is-none"></i>仅跳过或无记录</span>
        </div>
        <div class="dash-cal-foot">
          <span class="dash-cal-note" id="cal-note">{{ calNote }}</span>
        </div>
      </section>

      <!-- 账号与用户容量 -->
      <section class="col-6 card dash-card--fit">
        <div class="panel-head">
          <div class="panel-head-row">
            <h2 class="panel-title">账号与用户容量</h2>
          </div>
          <p class="panel-sub">改动上限在「系统设置 → 容量配额」；容量估算按当前签到窗口与账号间隔计算。</p>
        </div>
        <div class="dash-health" id="capacity-rows">
          <div class="dash-health-row">
            <div class="dash-health-label"><svg aria-hidden="true"><use href="#i-list" /></svg><span>账号容量</span></div>
            <div class="dash-health-value" id="capacity-accounts">
              <span v-if="settingsState === 'loading'" class="skeleton skeleton--text dash-skel" aria-hidden="true"></span>
              <span v-else-if="settingsState === 'fail'" class="dash-error-text">加载失败</span>
              <template v-else>{{ capRows?.accounts }}</template>
            </div>
          </div>
          <div class="dash-health-row">
            <div class="dash-health-label"><svg aria-hidden="true"><use href="#i-user" /></svg><span>用户容量</span></div>
            <div class="dash-health-value" id="capacity-users">
              <span v-if="settingsState === 'loading'" class="skeleton skeleton--text dash-skel" aria-hidden="true"></span>
              <span v-else-if="settingsState === 'fail'" class="dash-error-text">加载失败</span>
              <template v-else>{{ capRows?.users }}</template>
            </div>
          </div>
          <div class="dash-health-row">
            <div class="dash-health-label"><svg aria-hidden="true"><use href="#i-chart-bar" /></svg><span>容量估算</span></div>
            <div class="dash-health-value" id="capacity-estimate">
              <span v-if="settingsState === 'loading'" class="skeleton skeleton--text dash-skel" aria-hidden="true"></span>
              <span v-else-if="settingsState === 'fail'" class="dash-error-text">加载失败</span>
              <template v-else>{{ capRows?.estimate }}</template>
            </div>
          </div>
        </div>
        <p class="dash-cap-links">
          <a class="linklike" :href="BASE + '/work/accounts'">排查账号异常 →</a>
          <a class="linklike" :href="BASE + '/data/logs'">排查签到日志 →</a>
        </p>
      </section>

      <!-- 运行状态与公告 -->
      <section class="col-12 card">
        <div class="panel-head">
          <div class="panel-head-row">
            <h2 class="panel-title">运行状态与公告</h2>
            <button type="button" class="btn btn--ghost btn--sm" id="ping-btn" :disabled="pingState === 'loading'" @click="doPing">
              <svg aria-hidden="true"><use href="#i-activity" /></svg><span>检测易班接口</span>
            </button>
          </div>
        </div>

        <div class="dash-health">
          <div class="dash-health-row">
            <div class="dash-health-label"><svg aria-hidden="true"><use href="#i-clock" /></svg><span>服务器时钟</span></div>
            <div class="dash-health-value" id="health-clock">
              <span v-if="clockState === 'loading'" class="skeleton skeleton--text dash-skel" aria-hidden="true"></span>
              <span v-else-if="clockState === 'fail'" class="dash-error-text">时间校准失败</span>
              <template v-else>
                <span class="badge dot" :class="clock.tone">{{ clock.text }}</span>
                <span class="dash-health-text">{{ clock.detail }}</span>
              </template>
            </div>
          </div>
          <div class="dash-health-row">
            <div class="dash-health-label"><svg aria-hidden="true"><use href="#i-server" /></svg><span>易班接口连通性</span></div>
            <div class="dash-health-value" id="health-ping">
              <span v-if="pingState === null" class="dash-muted">尚未检测</span>
              <span v-else-if="pingState === 'loading'" class="spinner sm" aria-hidden="true"></span>
              <template v-else>
                <span class="badge dot" :class="pingState.ok ? 'badge--ok' : 'badge--bad'">{{ pingState.text }}</span>
                <span v-if="pingState.detail" class="dash-health-text">{{ pingState.detail }}</span>
              </template>
            </div>
          </div>
          <div class="dash-health-row">
            <div class="dash-health-label"><svg aria-hidden="true"><use href="#i-pause" /></svg><span>全局签到暂停</span></div>
            <div class="dash-health-value" id="health-global-pause">
              <span v-if="settingsState === 'loading'" class="skeleton skeleton--text dash-skel" aria-hidden="true"></span>
              <span v-else-if="settingsState === 'fail'" class="dash-error-text">读取失败</span>
              <span v-else class="badge dot" :class="'badge--' + pause.global.tone">{{ pause.global.text }}</span>
            </div>
          </div>
          <div class="dash-health-row">
            <div class="dash-health-label"><svg aria-hidden="true"><use href="#i-user" /></svg><span>新用户注册暂停</span></div>
            <div class="dash-health-value" id="health-reg-pause">
              <span v-if="settingsState === 'loading'" class="skeleton skeleton--text dash-skel" aria-hidden="true"></span>
              <span v-else-if="settingsState === 'fail'" class="dash-error-text">读取失败</span>
              <span v-else class="badge dot" :class="'badge--' + pause.reg.tone">{{ pause.reg.text }}</span>
            </div>
          </div>
          <div class="dash-health-row dash-health-row--wrap">
            <div class="dash-health-label"><svg aria-hidden="true"><use href="#i-bell" /></svg><span>站点公告</span></div>
            <div class="dash-health-value" id="health-announcement">
              <span v-if="annState === 'loading'" class="skeleton skeleton--text dash-skel" aria-hidden="true"></span>
              <span v-else-if="annState === 'fail'" class="dash-error-text">公告加载失败</span>
              <span v-else-if="announcement.empty" class="dash-muted">{{ announcement.text }}</span>
              <span v-else class="dash-health-text">{{ announcement.text }}</span>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
