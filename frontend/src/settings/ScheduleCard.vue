<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { confirmDialog, toast } from "../lib/shell";
import {
  SCHEDULE_DEFAULTS,
  clampGap,
  distWarnText,
  edgeMaxMin,
  edgeVal,
  norm,
  pctVal,
  scheduleBody,
  scheduleFormSnapshot,
  scheduleSnapshot,
  scheduleWarnText,
  windowSec,
} from "./model.js";
import DistViz from "./DistViz.vue";

/* 签到调度卡（设置页第一分区）。legacy = components/settings-schedule.js + settings-dist-viz.js。
   自研控件四件套按既定方向换 EP 基础件：排序/分布 → el-select；掐头去尾 → el-slider；
   签到窗口 → el-time-picker(is-range)；周末签到与自选开关 → el-switch（A15 统一为开关）。
   正态 μ/σ 的可见编辑器仍由 DistViz 构建（散布/半径是原生 number input；峰值中心已换
   EP el-time-picker，提交式预览）。

   2026-10-04 P3 收官重设计（本文件）：
   · 布局——字段按语义重排，≤720 单列 / 721–1439 双列 / ≥1440 三列（密度随视口放大）；
   · A15——周六/周日/自选三个布尔开关合并进同一列并各自带标注（原来两段散点、布尔控件
     混用 checkbox 与 switch 两套外观）；
   · EP 精修——el-select / el-time-picker 与 .input 同高（经 .settings-page 单点规则）。
   交互与口径层（保存语义/档位/脏守卫/警示）一字未动。

   权限（单源是后端档位表）：A 档（窗口/裁剪/周末/间隔/μσ）仅主管理员；B 档（排序/分布/自选）
   任意管理员可改。禁用只是界面口径，后端仍逐字段 403 + 口令门。

   直接操作门（工单 4gvh，用户 2026-10-04 口径）：本卡的直接操作控件（正态钟形画布的指针
   拖拽与方向键、掐头去尾两枚滑杆）一律默认只读，按下卡头「编辑」按钮才可操作，以降低窄屏
   误触；按钮自证（编辑 ⇄ 完成 + aria-pressed），不加解释文案。门是真状态：只读时画布吃不到
   指针事件、滑杆真禁用。可见的 μ/σ 数字编辑器不在门内——逐键键入不是误触形状。

   保存语义：改动只标脏，点「保存调度设置」才提交（只送相对快照变化的字段）；有改动就走
   受门禁 helper（先不带凭据发，后端 reason 决定补口令）。门不改变这条语义：编辑态改完仍走
   同一枚保存按钮与同一道口令门，刷新后回读一致。 */

const props = defineProps<{ settings: Record<string, unknown>; isMaster: boolean }>();

const snap = ref(scheduleSnapshot({}));
const form = ref({
  order: SCHEDULE_DEFAULTS.order,
  dist: SCHEDULE_DEFAULTS.dist,
  edgeFront: 1,
  edgeBack: 1,
  gap: 10,
  pref: false,
  sat: false,
  sun: false,
  windowStart: SCHEDULE_DEFAULTS.start,
  windowEnd: SCHEDULE_DEFAULTS.end,
  muLo: SCHEDULE_DEFAULTS.muMin,
  muHi: SCHEDULE_DEFAULTS.muMax,
  sgLo: SCHEDULE_DEFAULTS.sigmaMin,
  sgHi: SCHEDULE_DEFAULTS.sigmaMax,
});
const tip = ref<{ text: string; bad: boolean }>({ text: "", bad: false });
const windowFallback = ref("");
const saving = ref(false);
/* 直接操作门（工单 4gvh，用户 2026-10-04 口径）：本卡的"直接操作控件"——正态钟形画布
   （指针拖拽/方向键）与掐头去尾两枚滑杆——默认只读，按下卡头「编辑」才可操作，以降低
   窄屏误触。门是**真状态**：只读时画布吃不到指针事件、滑杆真禁用（不是视觉覆盖）。 */
const editing = ref(false);
const gateOpen = computed(() => props.isMaster && editing.value);

const orderItems = [
  { value: "sequence", label: "列表顺序（固定作息）" },
  { value: "random", label: "列表随机（每天重排）" },
];
const distItems = [
  { value: "front", label: "提前铺完（推荐）" },
  { value: "uniform", label: "均匀分布" },
  { value: "normal", label: "正态分布（钟形拟人）" },
];

const winSec = computed(() => windowSec(form.value.windowStart, form.value.windowEnd));
const edgeMax = computed(() => edgeMaxMin(winSec.value));
// 窗口无效（宽 <= 0）时缓冲上限为 0：滑杆量程不可为零宽（EP 会按 0/0 算出 NaN/Infinity），
// 此时给一个占位量程并把滑杆禁用，配合下方告警文案引导先修正窗口。
const edgeMaxValid = computed(() => edgeMax.value > 0);
const edgeSliderMax = computed(() => (edgeMaxValid.value ? edgeMax.value : 1));
const edgeSliderDisabled = computed(() => !props.isMaster || !edgeMaxValid.value || !editing.value);
const capacityN = computed(() => {
  const cap = (props.settings.capacity_estimate || {}) as { current_accounts?: number };
  const n = Number(cap.current_accounts);
  return isFinite(n) && n > 0 ? n : 0;
});
const dirty = computed(() => Object.keys(scheduleBody(snap.value, form.value)).length > 0);

// 滑块量程随窗口动态收窄；旧值超出上限时保存会被服务端夹取，故就地预警（纯展示、不阻断）。
// 窗口无效时滑杆已禁用，额外给一条明确告警（服务端 fallback 文案可能在，排在前面）。
const edgeWarn = computed(() => {
  const base = scheduleWarnText({
    fallbackText: windowFallback.value,
    frontSec: edgeVal(form.value.edgeFront),
    backSec: edgeVal(form.value.edgeBack),
    gap: clampGap(form.value.gap),
    winSec: winSec.value,
    n: capacityN.value,
    maxMin: edgeMax.value,
  });
  if (edgeMaxValid.value) return base;
  return (base ? base + "；" : "") + "签到窗口无效（开始须早于结束）：掐头去尾已停用，请先修正窗口。";
});
const muWarn = computed(() => distWarnText(pctVal(form.value.muLo, 40), pctVal(form.value.muHi, 60), "mu"));
const sigmaWarn = computed(() => distWarnText(pctVal(form.value.sgLo, 15), pctVal(form.value.sgHi, 25), "sigma"));

// DistViz 的实时上下文：窗口/掐头去尾（含 20% 钳位）、分布方式、账号数
const distCtx = computed(() => {
  const capMin = edgeMaxMin(winSec.value);
  const frontMin = Math.min(edgeVal(form.value.edgeFront) / 60, capMin);
  const backMin = Math.min(edgeVal(form.value.edgeBack) / 60, capMin);
  const sMin = form.value.windowStart.split(":").map((x) => parseInt(x, 10) || 0);
  const eMin = form.value.windowEnd.split(":").map((x) => parseInt(x, 10) || 0);
  const startMin = sMin[0] * 60 + sMin[1];
  const endMin = eMin[0] * 60 + eMin[1];
  return {
    effLo: startMin + frontMin,
    effHi: endMin - backMin,
    span: (winSec.value - (frontMin + backMin) * 60) / 60,
    frontMin,
    backMin,
    dist: form.value.dist,
    n: capacityN.value,
  };
});
const muModel = computed(() => ({ muLo: form.value.muLo, muHi: form.value.muHi, sgLo: form.value.sgLo, sgHi: form.value.sgHi }));

function apply(data: Record<string, unknown>): void {
  windowFallback.value = String(data.window_fallback_text || "");
  snap.value = scheduleSnapshot(data);
  const s = snap.value;
  const parts = String(s.window).split("~");
  form.value = {
    order: s.order,
    dist: s.dist,
    edgeFront: s.edgeFront / 60,
    edgeBack: s.edgeBack / 60,
    gap: s.gap,
    pref: !!s.pref,
    sat: !!s.sat,
    sun: !!s.sun,
    windowStart: norm(parts[0], SCHEDULE_DEFAULTS.start),
    windowEnd: norm(parts[1], SCHEDULE_DEFAULTS.end),
    muLo: s.muMin,
    muHi: s.muMax,
    sgLo: s.sigmaMin,
    sgHi: s.sigmaMax,
  };
  tip.value = { text: "", bad: false };
}
watch(() => props.settings, (v) => apply(v || {}), { immediate: true, deep: false });

function onGapChange(): void {
  form.value.gap = clampGap(form.value.gap);
}
function onWindowChange(v: unknown): void {
  if (Array.isArray(v) && v[0] && v[1]) {
    form.value.windowStart = String(v[0]);
    form.value.windowEnd = String(v[1]);
  }
}
function setMu(v: { muLo: number; muHi: number; sgLo: number; sgHi: number }): void {
  form.value.muLo = v.muLo;
  form.value.muHi = v.muHi;
  form.value.sgLo = v.sgLo;
  form.value.sgHi = v.sgHi;
}

async function save(): Promise<boolean> {
  if (saving.value) return false;
  const body = scheduleBody(snap.value, form.value);
  if (!Object.keys(body).length) {
    toast().info("没有需要保存的改动");
    return true;
  }
  const ops = (window as { YB?: { settingsOps?: Record<string, (c: unknown, b: unknown) => Promise<boolean>> } }).YB?.settingsOps;
  if (!ops) return false;
  saving.value = true;
  const ok = await ops.scheduleSave(
    { isMaster: props.isMaster, tip: (t: string, b: boolean) => (tip.value = { text: t, bad: b }) },
    body,
  );
  saving.value = false;
  if (ok) snap.value = scheduleFormSnapshot(form.value);
  return ok;
}
function reset(): void {
  if (!props.isMaster || saving.value) return;
  void confirmDialog({
    title: "恢复默认调度",
    body: "恢复为：窗口 06:30 ~ 07:50 · 掐头去尾各 1 分钟 · 排序顺序 · 分布提前铺完。\n（账号间隔与自选开关不在恢复范围，可点保存生效）",
    confirmText: "恢复默认",
  }).then((ok) => {
    if (!ok) return;
    form.value.order = SCHEDULE_DEFAULTS.order;
    form.value.dist = SCHEDULE_DEFAULTS.dist;
    form.value.edgeFront = 1;
    form.value.edgeBack = 1;
    form.value.windowStart = SCHEDULE_DEFAULTS.start;
    form.value.windowEnd = SCHEDULE_DEFAULTS.end;
    // 恢复本身不落盘（与全站「改动需点保存」一致），但没有可见反馈会读成"点了没反应"，
    // 故给就地文案；与保存成功共用 set-tip 载体，可区分"刚发生的事"。
    tip.value = dirty.value
      ? { text: "已恢复默认值，点「保存调度设置」后生效", bad: false }
      : { text: "当前已是默认值", bad: false };
  });
}

defineExpose({ isDirty: () => dirty.value, save });
</script>

<template>
  <section class="card" id="set-schedule">
    <div class="panel-head">
      <div class="panel-head-row">
        <h2 class="panel-title">签到调度</h2>
        <div class="set-head-actions">
          <button type="button" class="info-tip" aria-label="调度说明" aria-describedby="set-pop-schedule">
            <svg aria-hidden="true"><use href="#i-info" /></svg>
            <span class="info-pop" id="set-pop-schedule" role="tooltip"><b>排序与分布：</b>「顺序」按列表先到先签，「随机」每天重排；分布决定时间点怎么铺开：「提前铺完」把账号铺进窗口前段、尾部留作重试与兜底，「均匀」铺满整窗，「正态」钟形拟人。<b>掐头去尾：</b>裁掉窗口首尾各 n 分钟，避开边界超时；前后独立、0.5 分钟粒度。<b>账号间隔：</b>相邻两次签到请求的最小间隔（秒），自动与手动均生效，0=关闭；调大可降低同一 IP 连续登录被风控的概率，但占用更多窗口时间。<b>生效时机：</b>保存后下次自动签到时生效。</span>
          </button>
          <!-- 直接操作门开关（工单 4gvh）：拖拽类控件默认只读，按下此处才可操作。
               按钮自证（编辑 ⇄ 完成 + aria-pressed），不加解释文案。 -->
          <button
            type="button"
            class="btn btn--ghost btn--sm"
            id="ss-edit"
            data-sched-edit
            :aria-pressed="editing ? 'true' : 'false'"
            :disabled="!isMaster"
            @click="editing = !editing"
          >{{ editing ? "完成" : "编辑" }}</button>
          <span class="badge badge--warn" id="ss-dirty" :hidden="!dirty">有未保存的修改</span>
        </div>
      </div>
    </div>
    <p class="alert info" id="ss-perm" :hidden="isMaster">
      <span class="ico"><svg aria-hidden="true"><use href="#i-info" /></svg></span>
      <span class="body">仅主管理员可改核心配置：签到窗口、掐头去尾、账号间隔、周末开关与正态分布。排序方式、分布方式与「允许用户自选签到时间」任意管理员可改，保存时仍需口令确认。</span>
    </p>
    <div class="form-grid">
      <!-- 字段顺序按语义重排（方式 → 窗口 → 正态参数 → 开关），并挂密度类名：≤720 单列 /
           721–1439 双列 / ≥1440 三列。大视口升列是为了不让控件被拉成 780px 空框
           （旧观感：一个短下拉铺满半张卡），密度随视口放大而不是锁 max-width。 -->
      <div class="field sched-f-order">
        <span class="field-label" id="ss-order-label">排序方式</span>
        <!-- data-select-field 是两栈共通的 e2e/契约锚点（legacy 自研控件的根也用它） -->
        <div class="select-field" data-select-field="ss-order">
          <el-select id="ss-order" v-model="form.order" aria-label="排序方式" style="width: 100%">
            <el-option v-for="it in orderItems" :key="it.value" :value="it.value" :label="it.label" />
          </el-select>
        </div>
      </div>
      <div class="field sched-f-dist">
        <span class="field-label" id="ss-dist-label">分布方式</span>
        <div class="select-field" data-select-field="ss-dist">
          <el-select id="ss-dist" v-model="form.dist" aria-label="分布方式" style="width: 100%">
            <el-option v-for="it in distItems" :key="it.value" :value="it.value" :label="it.label" />
          </el-select>
        </div>
      </div>
      <div class="field sched-f-window">
        <span class="field-label" id="ss-window-label">签到窗口</span>
        <div class="time-pair" data-time-pair>
          <!-- 当前值文本（读屏可及 + 两栈共通锚点）：legacy 触发器本身是文本按钮，
               EP 是输入框（值不进 innerText），故这里补一份 sr-only 文本供 e2e/读屏取。 -->
          <span class="sr-only" data-window-text>{{ form.windowStart }} 至 {{ form.windowEnd }}</span>
          <el-time-picker
            :model-value="[form.windowStart, form.windowEnd]"
            is-range
            format="HH:mm"
            value-format="HH:mm"
            range-separator="至"
            start-placeholder="开始"
            end-placeholder="结束"
            aria-label="签到窗口"
            :disabled="!isMaster"
            style="width: 100%"
            @update:model-value="onWindowChange"
          />
        </div>
        <p class="field-help">开始必须早于结束；保存后下次自动签到生效。</p>
      </div>
      <div class="field field--narrow sched-f-gap">
        <span class="field-label" id="ss-gap-label">账号间隔</span>
        <div class="input-group">
          <input id="ss-gap" v-model.number="form.gap" class="input" type="number" min="0" max="3600" placeholder="10" aria-labelledby="ss-gap-label" :disabled="!isMaster" @change="onGapChange" />
          <span class="addon">秒</span>
        </div>
        <p class="field-help">自动手动均生效，0 = 关闭；修改需当前管理员密码确认。</p>
      </div>
      <div class="field sched-f-front">
        <span class="field-label" id="ss-edge-front-label">掐头</span>
        <div class="sched-range" data-range-field="ss-edge-front">
          <el-slider v-model="form.edgeFront" :min="0" :max="edgeSliderMax" :step="0.5" :disabled="edgeSliderDisabled" />
        </div>
        <p class="field-help">{{ form.edgeFront }} 分钟（单边上限 {{ edgeMax }} 分钟）</p>
      </div>
      <div class="field sched-f-back">
        <span class="field-label" id="ss-edge-back-label">去尾</span>
        <div class="sched-range" data-range-field="ss-edge-back">
          <el-slider v-model="form.edgeBack" :min="0" :max="edgeSliderMax" :step="0.5" :disabled="edgeSliderDisabled" />
        </div>
        <p class="field-help">{{ form.edgeBack }} 分钟（单边上限 {{ edgeMax }} 分钟）</p>
      </div>
      <div class="field span-2">
        <span class="field-label" id="ss-mu-label">正态分布（峰值中心与散布）</span>
        <DistViz :model-value="muModel" :ctx-data="distCtx" :readonly="!isMaster" :edit-gate="gateOpen" @update:model-value="setMu" />
        <p class="set-warn" id="ss-mu-warn" :hidden="!muWarn">{{ muWarn }}</p>
        <p class="set-warn" id="ss-sigma-warn" :hidden="!sigmaWarn">{{ sigmaWarn }}</p>
      </div>
      <!-- A15：三个布尔开关（周六 / 周日 / 自选）合并进同一列并各自带标注。
           原先「周六/周日复选框行 + 自选开关行」两段散点：布尔控件混用 checkbox 与 switch
           两套外观（类同原则的过度项），且行的左右缘与字段列不齐。 -->
      <div class="field sched-f-switches">
        <span class="field-label" id="ss-weekend-label">周末与自选</span>
        <div class="sched-switches">
          <div class="set-row">
            <label class="set-row-text" for="ss-sat">
              <span class="set-row-label">周六签到</span>
            </label>
            <el-switch id="ss-sat" v-model="form.sat" :disabled="!isMaster" aria-label="周六签到" />
          </div>
          <div class="set-row">
            <label class="set-row-text" for="ss-sun">
              <span class="set-row-label">周日签到</span>
            </label>
            <el-switch id="ss-sun" v-model="form.sun" :disabled="!isMaster" aria-label="周日签到" />
          </div>
          <div class="set-row">
            <label class="set-row-text" for="ss-time-pref">
              <span class="set-row-label">允许用户自选签到时间</span>
            </label>
            <el-switch id="ss-time-pref" v-model="form.pref" aria-label="允许用户自选签到时间" />
          </div>
        </div>
        <p class="field-help">周末签到默认关闭；自选开启后用户可选 5 分钟时间片，关闭时选择可存但不生效。</p>
      </div>
    </div>
    <p class="set-warn" id="ss-edge-warn" :hidden="!edgeWarn">{{ edgeWarn }}</p>

    <p class="set-tip" :class="{ 'set-bad': tip.bad }" id="ss-tip" role="status">{{ tip.text }}</p>

    <div class="form-actions is-sticky">
      <button type="button" class="btn btn--primary btn--sm" id="ss-save" :disabled="!dirty || saving" @click="save()">保存调度设置</button>
      <span class="spacer" />
      <button type="button" class="btn btn--ghost btn--sm" id="ss-reset" :disabled="!isMaster || saving" @click="reset">恢复默认调度</button>
    </div>
  </section>
</template>
