<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { api, swapMs, swapOut } from "../lib/shell";
import {
  ANIM_MIN_MS,
  BLANK_CLS,
  CAL_API,
  CELLS,
  SKELETON_CLS,
  WEEK,
  gridCells,
  monthDirection,
  monthKey,
  monthLabel,
  shiftMonth,
  todayStr,
} from "./model.js";
import type { CalendarCtx, CalendarFlags, CalendarPayload } from "./types";

/* 单个账号的月历卡（每个生效账号一张）。

   与 legacy `web/static/js/calendar.js` 的分工：格子口径（底色类名、读屏名、周末角标）
   搬到 `model.js`（纯 JS，Python 的 Node 对拍测试仍在跑），换月动效与取数编排留在本组件。
   本组件是 `data-sc-date` 的**唯一**产出方（tests/test_web_calendar_parity.py 钉住这一点）。

   ★ 五个必须保真的反直觉点（逐条对应 legacy 注释里踩过的坑）：
     1. 待取月份与**已渲染**月份分开：方向动效取「相对上次已渲染月份」的差，连续快点
        两次也按同一方向累计；数据落地才写回已渲染月份。
     2. 首次/出错后才换骨架格；换月期间保留旧格，等数据到了再做有方向的退出→换→进入
        （旧实现同帧摘类会取消退出过渡，内容只淡到 0.4–0.7 就被拉回，看着只是闪一下）。
     3. 短请求不播动效（ANIM_MIN_MS）：加载越短越不该动。
     4. 默认选中今天是"无人操作时"的兜底：选中是响应式状态，请求在途时用户已点选的话，
        数据落地时读到的是他的选择，绝不会把用户的日期改回今天。
     5. 换月时保留用户当前选中态（选中日期可以落在别的月份，此时无格高亮，是正确的）。
     6. 取数失败后再重试：**保留上一次成功月份的内容**（不是骨架格）——月份标题与格子
        一起停在旧月份、`aria-busy` 表示在取，两者始终自洽；legacy 在重试时改铺骨架格，
        但那会让标题与内容短暂不同步（标题还停在旧月份）。 */

const props = defineProps<{
  phone: string;
  ctx: CalendarCtx;
}>();

const emit = defineEmits<{
  (e: "select", date: string, auto: boolean): void;
  /** 服务端下发的周末开关：日志面板要用同一份真值判断"今天无需签到"。 */
  (e: "flags", flags: CalendarFlags): void;
  /** 本月真正显示出来的语气档（图例按并集收敛）。 */
  (e: "tones", tones: string[]): void;
}>();

const now = new Date();
const today = todayStr();
const SWAP_MS = swapMs();

/** 待取月份（点击即改）与已渲染月份（数据落地才改）——见 ★1。 */
const pend = ref({ year: now.getFullYear(), month: now.getMonth() + 1 });
const view = ref({ year: pend.value.year, month: pend.value.month });
const viewKey = computed(() => monthKey(view.value.year, view.value.month));
const label = computed(() => monthLabel(view.value.year, view.value.month));

const data = ref<CalendarPayload | null>(null);
const flags = ref<CalendarFlags>({ saturday: false, sunday: false });
const selected = ref("");
const loading = ref(false);
const errorText = ref("");

const gridEl = ref<HTMLElement | null>(null);
const labelEl = ref<HTMLElement | null>(null);

/* 格子由「数据 + 选中态」派生：点选只改 selected，不重取、不重播换月动效
   （选中态自带 CSS 过渡）。 */
const grid = computed(() =>
  gridCells({
    data: data.value ?? {},
    phone: props.phone,
    year: view.value.year,
    month: view.value.month,
    selected: selected.value,
    today,
    flags: flags.value,
    ctx: props.ctx,
  }),
);
const cells = computed(() => grid.value.cells);
watch(
  () => grid.value.used.join(","),
  () => emit("tones", grid.value.used.slice()),
);

let seq = 0;
let shiftSeq = 0;

function reduceMotion(): boolean {
  return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function clearShift(): void {
  for (const n of [gridEl.value, labelEl.value]) {
    if (n) n.classList.remove("is-swapping", "is-shifting-in", "is-shift-next", "is-shift-prev");
  }
}

/** 换月：旧格保持到数据到达，再做一次有方向的「退出 → 换内容 → 进入」（只动 transform/opacity）。 */
function shift(dir: number, apply: () => void): void {
  const g = gridEl.value;
  const l = labelEl.value;
  if (!g || reduceMotion()) {
    clearShift();
    apply();
    return;
  }
  const my = ++shiftSeq;
  clearShift();
  g.classList.add(dir > 0 ? "is-shift-next" : "is-shift-prev");
  g.classList.add("is-swapping");
  if (l) l.classList.add("is-swapping");
  let done = false;
  const onEnd = (e: TransitionEvent): void => {
    if (e.target === g) enter();
  };
  function enter(): void {
    if (done) return;
    done = true;
    g?.removeEventListener("transitionend", onEnd);
    if (my !== shiftSeq) return; // 已被下一次切换取代
    apply();
    // 等 Vue 把新格挂上再摘类：摘类要在内容已换之后，才谈得上"进入过渡"
    void nextTick(() => {
      const node = gridEl.value;
      if (!node || my !== shiftSeq) return;
      node.classList.remove("is-swapping");
      labelEl.value?.classList.remove("is-swapping");
      node.classList.add("is-shifting-in");
      labelEl.value?.classList.add("is-shifting-in");
      void node.offsetWidth; // 提交起点（transition:none 下落位），摘类时才产生进入过渡
      requestAnimationFrame(() => {
        const n2 = gridEl.value;
        if (!n2) return;
        n2.classList.remove("is-shifting-in", "is-shift-next", "is-shift-prev");
        labelEl.value?.classList.remove("is-shifting-in");
      });
    });
  }
  g.addEventListener("transitionend", onEnd);
  setTimeout(enter, SWAP_MS);
}

/** 未换月的慢请求：整体淡出 → 换内容 → 淡入（复用 core.js 的 swapOut 口径）。 */
function slowSwap(apply: () => void): void {
  const g = gridEl.value;
  if (!g) {
    apply();
    return;
  }
  clearShift();
  const my = ++shiftSeq;
  swapOut([g, labelEl.value].filter(Boolean), () => {
    if (my !== shiftSeq) return;
    apply();
  });
}

async function render(selectDate?: string): Promise<void> {
  const my = ++seq;
  const year = pend.value.year;
  const month = pend.value.month;
  const key = monthKey(year, month);
  loading.value = true;
  errorText.value = "";
  const t0 = performance.now();
  let resp: CalendarPayload;
  try {
    resp = await api<CalendarPayload>("GET", `${CAL_API}?month=${key}`);
  } catch (e) {
    if (my !== seq) return; // 已被更晚的请求取代
    loading.value = false;
    errorText.value = (e as Error)?.message || "请稍后重试";
    clearShift(); // 失败态不留换月动效的中间类（legacy 同此）
    return;
  }
  if (my !== seq) return;
  const f: CalendarFlags = { sunday: !!resp.sunday_sign, saturday: resp.saturday_sign === 1 };
  flags.value = f;
  emit("flags", f);

  // 选中落地：显式日期 > 用户已有选择（含请求在途时的新点选）> 当月默认今天（★4）
  let sel = selectDate || selected.value;
  let auto = false;
  if (!sel && key === monthKey(now.getFullYear(), now.getMonth() + 1)) {
    sel = today;
    auto = true;
  }

  const slow = performance.now() - t0 > ANIM_MIN_MS;
  const dir = monthDirection(viewKey.value, year, month);
  const apply = (): void => {
    // 动效是延后执行的（等退出过渡跑完），期间可能又落了一次更新的请求：此时这次的结果
    // 已经过期，不能拿它覆盖（`shiftSeq` 只挡"换月被取代"，挡不住"同月重取"这类同序号场景）。
    if (my !== seq) return;
    data.value = resp;
    view.value = { year, month };
    selected.value = sel;
    loading.value = false;
    if (selectDate) emit("select", sel, false);
    else if (auto) emit("select", sel, true);
  };
  if (dir) shift(dir, apply);
  else if (slow) slowSwap(apply);
  else {
    // 快请求也要先清动效类（legacy 的快路径同样 clearShift）：上一次换月还在中途时
    // 又落一次同月数据，不清就会把 is-swapping / is-shift-* 留在格子上。
    clearShift();
    apply();
  }
}

function step(delta: number): void {
  pend.value = shiftMonth(pend.value, delta);
  void render();
}

function goToday(): void {
  const parts = today.split("-");
  pend.value = { year: Number(parts[0]), month: Number(parts[1]) };
  void render(today);
}

function pick(date: string): void {
  // 不做"同一天就跳过"的短路：重复点同一天要能**重新拉一次当天日志**（legacy 的 select
  // 每次都会 loadLog）——这是页面里唯一的手动刷新入口，去掉等于少一条退路。
  selected.value = date;
  emit("select", date, false);
}

onMounted(() => {
  void render();
});
</script>

<template>
  <div class="sc-mount" data-sc-mount>
    <div class="sc-toolbar">
      <div class="sc-toolbar-left">
        <h3 ref="labelEl" class="sc-month">{{ label }}</h3>
        <div class="sc-nav">
          <button
            type="button"
            class="btn btn--ghost btn--icon sc-nav-btn"
            data-sc-shift="-1"
            aria-label="上个月"
            @click="step(-1)"
          >
            <svg class="sc-ico" aria-hidden="true"><use href="#i-chevron-left" /></svg>
          </button>
          <button
            type="button"
            class="btn btn--ghost btn--icon sc-nav-btn"
            data-sc-shift="1"
            aria-label="下个月"
            @click="step(1)"
          >
            <svg class="sc-ico" aria-hidden="true"><use href="#i-chevron-right" /></svg>
          </button>
        </div>
      </div>
      <button type="button" class="btn btn--ghost btn--sm" data-sc-today @click="goToday">今天</button>
    </div>

    <div class="sc-weekdays" aria-hidden="true">
      <span v-for="w in WEEK" :key="w">{{ w }}</span>
    </div>

    <div ref="gridEl" class="sc-grid" data-sc-grid :aria-busy="loading ? 'true' : 'false'">
      <p v-if="errorText" class="sc-error">
        <span>日历加载失败：{{ errorText }}</span>
        <button type="button" class="btn btn--ghost btn--sm" data-sc-retry @click="render()">重试</button>
      </p>
      <template v-else-if="!data">
        <span v-for="i in CELLS" :key="'sk' + i" :class="SKELETON_CLS" aria-hidden="true"></span>
      </template>
      <template v-else>
        <template v-for="(cell, i) in cells" :key="'c' + i">
          <span v-if="!cell" :class="BLANK_CLS"></span>
          <button
            v-else
            type="button"
            :class="cell.cls"
            :data-sc-date="cell.date"
            :title="cell.date"
            :aria-label="cell.label"
            :aria-pressed="cell.pressed ? 'true' : 'false'"
            @click="pick(cell.date)"
          >
            <span class="sc-num">{{ cell.d }}</span>
            <i v-if="cell.offBadge" class="sc-off" aria-hidden="true">休</i>
          </button>
        </template>
      </template>
    </div>
  </div>
</template>
