<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, shellBase, toast } from "../lib/shell";
import SignCalendar from "./SignCalendar.vue";
import { LOG_API, logEmptyText, logSkipText, shouldAnimate, statusLine, unionTones } from "./model.js";
import type { AccountItem, CalendarCtx, CalendarFlags, LegendItem, MyLogsPayload } from "./types";

/* 「签到日历」页编排（/user/calendar 与 /my/calendar 共用的唯一实现）。

   与 legacy 的分工：页头 + 挂载点 + 内联状态载荷由 partials/page_sign_calendar.html 渲染
   （两端同一份），月历本体是 SignCalendar.vue，本组件只做**页面编排**：身份守卫 → 拉账号
   → 生成日历卡 → 共享日志面板 → 图例收敛。

   两页的差异是**配置**（角色守卫、拒绝去向、空态去向、页头文案）而非代码：页头与 data-*
   由服务端渲染，故不必复制一份 bundle（同一入口服务两页）。

   ★ 日志面板是**全页共享**的：多张卡往同一块面板写，且"多卡同时自动选中今天"只查一次
     接口（autoLoadedDate 去重）。

   ★ 与 legacy 的一处**有意取舍**：legacy 的日志面板与图例是**服务端渲染**的，理由是
     "JS 未执行时也留着'日志放这里'的位置"。迁到 Vue 后整页只剩挂载点，右列不再存在；
     无 JS 时由共享 partial 的 `<noscript>` 提示顶上。可接受的理由：本页的数据（账号列表、
     月历、日志）**本来就全靠客户端取**，服务端渲染的空壳除了占位之外没有任何信息量，
     而要在服务端也渲染一份两栏栅格，等于把布局实现成两份（正是本仓反复吃亏的形态）。

   ★ 状态图例与账号卡状态行消费**同一份状态表**：档位清单与中文短名由服务端
     `legend_items()` 随载荷下发（见 pages.py 的 _calendar_page_context），前端不另立一份；
     `data-tone` 只用于"只解释看得见的颜色"这一层收敛。 */

const props = defineProps<{
  role: "user" | "admin";
  denyHref: string;
  emptyHref: string;
  ctx: CalendarCtx;
}>();

const accounts = ref<AccountItem[]>([]);
const listLoading = ref(true);
const listError = ref("");

/** 服务端下发的周末开关（由日历卡回报；日志面板用同一份真值判断"今天无需签到"）。 */
const flags = ref<CalendarFlags>({ saturday: false, sunday: false });
/** 每张卡本月真正显示出来的语气档；图例按并集收敛（切月只重算该卡）。 */
const legendUsed = ref<Record<number, string[]>>({});
const usedTones = computed(() => unionTones(legendUsed.value));
const legendItems = computed<LegendItem[]>(() => props.ctx.legend ?? []);
/**
 * 图例收敛开关：**只有某张卡真的报过档位之后才收敛**。
 *
 * 无卡、或卡还在取数（还没报档位）时都不收敛——此时图例是页面上唯一能学到「哪种颜色代表
 * 什么」的地方，全量陈列才是有信息的；legacy 的实际行为也是这一种（`trimLegend` 只在日历
 * 渲染成功后调用，没人调就没人收敛）。有卡报过之后只解释当前看得见的颜色：没出现的档位
 * 条目是纯噪音（语气档全量清单是服务端的事，见 pages.py）。
 *
 * 判据取"有没有卡报过"而不是"有没有账号"：后者在 /api/my-accounts 返回后、日历数据回来前
 * 会有一段"有账号但无档位"的窗口，那时收敛会把 5 个档位全隐藏掉（legacy 不会）。
 * 取前者时该窗口自然保持全量；若日后账号列表重拉清空了已报档位，失效方向也是"多显示"而非
 * "全隐藏"，不会把图例变成空白。
 */
const trimLegend = computed(() => Object.keys(legendUsed.value).length > 0);

type LogKind = "placeholder" | "skip" | "empty" | "text" | "error";
const logDate = ref("选择日期查看当天记录");
const logKind = ref<LogKind>("placeholder");
const logSkipReason = ref("");
const logText = ref("");
const logLoading = ref(false);
const logFresh = ref(false);
const logBox = ref<HTMLElement | null>(null);
const logCard = ref<HTMLElement | null>(null);
/** 已自动加载过日志的日期：多卡同时自动选中今天时只发一次请求。 */
const autoLoadedDate = ref("");

let logSeq = 0;

const empty = computed(() => !listLoading.value && accounts.value.length === 0);
const solo = computed(() => empty.value || !!listError.value);
const showLog = computed(() => !solo.value);
const logEmptyMessage = computed(() => {
  if (logKind.value === "skip") return logSkipReason.value;
  if (logKind.value === "error") return "读取失败，请稍后重试";
  if (logKind.value === "placeholder") return "点击日历中的日期，查看当天签到记录";
  return logEmptyText(logDate.value);
});

function statusOf(a: AccountItem): { cls: string; text: string } {
  return statusLine(a, props.ctx);
}

/* ---------------- 日志面板 ---------------- */

/** 堆叠布局（窄屏）下日志面板在日历下方、可能在视口外：选中日期后就地把它带进视野，
    否则"点了日期却什么都没发生"（变化盲）。已在视野内时不动，避免无谓滚动。 */
function revealLog(): void {
  const card = logCard.value;
  if (!card) return;
  const r = card.getBoundingClientRect();
  const vh = window.innerHeight || document.documentElement.clientHeight;
  if (r.top < vh * 0.9 && r.bottom > 0) return; // 已经看得到
  const reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  try {
    card.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
  } catch {
    card.scrollIntoView();
  }
}

/** 慢请求的内容替换做一次淡入；`is-fresh` 必须等动画跑完再摘（提前摘会截断动画）。 */
function flashFresh(): void {
  logFresh.value = true;
  const box = logBox.value;
  let done = false;
  const finish = (): void => {
    if (done) return;
    done = true;
    box?.removeEventListener("animationend", finish);
    logFresh.value = false;
  };
  box?.addEventListener("animationend", finish);
  setTimeout(finish, 200);
}

async function loadLog(date: string): Promise<void> {
  const my = ++logSeq;
  logDate.value = date;
  // 停签日无需签到：直接提示，不查日志（与日历置灰同一份周末开关）
  const skip = logSkipText(date, flags.value);
  if (skip) {
    logSkipReason.value = skip;
    logKind.value = "skip";
    logText.value = "";
    logLoading.value = false;
    return;
  }
  // 取数期间**不替换内容**（否则"加载中 → 结果"会让面板高度与位置跳一下）：只降透明度
  logLoading.value = true;
  const t0 = performance.now();
  try {
    const resp = await api<MyLogsPayload>("GET", `${LOG_API}?date=${date}`);
    if (my !== logSeq) return;
    const lines = resp.logs ?? [];
    if (lines.length) {
      logKind.value = "text";
      logText.value = lines.join("\n");
    } else {
      logKind.value = "empty";
      logText.value = "";
    }
    logLoading.value = false;
    if (shouldAnimate(performance.now() - t0)) flashFresh();
  } catch (e) {
    if (my !== logSeq) return;
    logKind.value = "error";
    logText.value = "";
    logLoading.value = false;
    toast().error((e as Error)?.message || "日志加载失败");
  }
}

function onSelect(date: string, auto: boolean): void {
  if (auto) {
    if (autoLoadedDate.value === date) return; // 全页共享面板：只查一次
    autoLoadedDate.value = date;
  }
  void loadLog(date);
  if (!auto) revealLog(); // 用户主动选（点格 / 今天）→ 就地把它带进视野
}

function onTones(index: number, tones: string[]): void {
  legendUsed.value = { ...legendUsed.value, [index]: tones };
}

function onFlags(next: CalendarFlags): void {
  flags.value = next;
}

/* ---------------- 账号列表 ---------------- */
async function loadAccounts(): Promise<void> {
  listLoading.value = true;
  listError.value = "";
  try {
    const data = await api<{ accounts?: AccountItem[] }>("GET", "/api/my-accounts");
    // 只回显已生效账号（pending/rejected/软删除不在这里出现，与接口同口径）
    accounts.value = (data?.accounts ?? []).filter((a) => !a.deleted && a.status === "active");
    legendUsed.value = {};
  } catch (e) {
    accounts.value = [];
    listError.value = "账号列表加载失败，请稍后重试。";
    toast().error((e as Error)?.message || "账号列表加载失败");
  } finally {
    listLoading.value = false;
  }
}

onMounted(async () => {
  // 服务端已按角色守卫，这里再核对一次：若会话在页面渲染与取数之间被换掉（例如另开标签页
  // 登录了别的角色），不至于把两类页面渲染成对方的形态。
  // denyHref 由服务端渲染时已带 request.script_root 前缀（子路径部署的唯一收口点在模板），
  // 故此处直接跳转、不再自己拼前缀（拼两次会变成 /prefix/prefix/...）。
  try {
    const me = await api<{ role?: string }>("GET", "/api/me");
    if (me?.role !== props.role) {
      window.location.href = props.denyHref;
      return;
    }
  } catch {
    window.location.href = shellBase() + "/login";
    return;
  }
  await loadAccounts();
});
</script>

<template>
  <div class="user-grid cal-layout" :class="{ 'is-solo': solo }">
    <!-- 左侧：账号日历卡；初始占位卡与真实日历卡同高（避免数据到达时左列高度跳变） -->
    <div class="col-6 cal-column" data-cal-list>
      <section v-if="listLoading" class="card cal-placeholder">
        <p class="empty">
          <span class="empty__icon"><svg aria-hidden="true"><use href="#i-calendar" /></svg></span>
          <span class="empty__msg">正在加载账号…</span>
        </p>
      </section>

      <section v-else-if="listError" class="card">
        <div class="empty">
          <span class="empty__msg">{{ listError }}</span>
          <span class="empty__action">
            <button type="button" class="btn btn--ghost btn--sm" @click="loadAccounts">重试</button>
          </span>
        </div>
      </section>

      <section v-else-if="empty" class="card">
        <div class="empty">
          <span class="empty__icon"><svg aria-hidden="true"><use href="#i-calendar" /></svg></span>
          <span class="empty__msg">还没有生效的账号。提交账号并通过管理员审核后，这里会显示签到日历。</span>
          <span class="empty__action">
            <a class="btn btn--primary" :href="emptyHref">去提交账号</a>
          </span>
        </div>
      </section>

      <template v-else>
        <section v-for="(a, i) in accounts" :key="i" class="card cal-card">
          <div class="panel-head">
            <div class="panel-head-row">
              <h2 class="panel-title">{{ a.display_name }}</h2>
            </div>
            <p class="panel-sub">{{ a.phone }}{{ a.phone_model ? " · " + a.phone_model : "" }}</p>
            <p class="panel-sub" :class="statusOf(a).cls">{{ statusOf(a).text }}</p>
          </div>
          <SignCalendar
            :phone="a.phone"
            :ctx="ctx"
            @select="onSelect"
            @flags="onFlags"
            @tones="onTones(i, $event)"
          />
        </section>
      </template>
    </div>

    <!-- 右侧：记录面板（常驻，保证"日志显示区"始终可见；宽屏两栏等高）。
         空态用全站统一 .empty 构件（§12 空状态），不再自造 .sc-empty（图标/字号与别页不一致）。 -->
    <section v-if="showLog" ref="logCard" class="card col-6 sc-log-card" data-sc-log-card>
      <div class="panel-head">
        <div class="panel-head-row">
          <h2 class="panel-title">记录</h2>
        </div>
        <p class="panel-sub" data-sc-log-date>{{ logDate }}</p>
      </div>
      <div
        ref="logBox"
        class="sc-log"
        data-sc-log
        aria-live="polite"
        :class="{ 'is-loading': logLoading, 'is-fresh': logFresh }"
      >
        <pre v-if="logKind === 'text'" class="log-view sc-log-text">{{ logText }}</pre>
        <div v-else class="empty">
          <span class="empty__icon"><svg aria-hidden="true"><use href="#i-calendar" /></svg></span>
          <span class="empty__msg">{{ logEmptyMessage }}</span>
          <span v-if="logKind === 'placeholder'" class="empty__msg">签到结果在每天调度后写入</span>
        </div>
      </div>
    </section>

    <!-- 图例：整行独占（col-12）。不能写成 col-6——日志面板隐藏时它会被排到空卡片右侧
         同一行，留下 6 列死区（实测空卡 472px、图例 x=776 且同 y）。 -->
    <ul class="col-12 sc-legend" aria-label="签到状态图例">
      <li
        v-for="item in legendItems"
        :key="item.tone"
        :data-tone="item.tone"
        :hidden="trimLegend && !usedTones[item.tone]"
      >
        <i class="sc-swatch" :class="`sc-swatch--${item.tone}`" aria-hidden="true"></i>{{ item.label }}
      </li>
      <li><i class="sc-swatch sc-swatch--off" aria-hidden="true">休</i>周末停签</li>
      <li><i class="sc-swatch sc-swatch--today" aria-hidden="true"></i>今天</li>
    </ul>
  </div>
</template>
