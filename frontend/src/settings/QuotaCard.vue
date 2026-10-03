<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { adviceLine, clockText, quotaBody, quotaSnapshot } from "./model.js";

/* 容量配额分区：上限编辑（仅主管理员）+ 容量建议与耗时实测。
   legacy = components/settings-quota.js。实测只判主管理员 + 后端全局冷却（429 + 倒计时），
   **不带口令框**——后端判它与手动签到同档（不改配置就不校验口令），前端不造"假门"。 */

const props = defineProps<{
  settings: Record<string, unknown>;
  isMaster: boolean;
  advice: Record<string, unknown> | null;
  onSaved: () => void;
}>();

const snap = ref({ users: 0, accounts: 0 });
const form = ref({ users: 0, accounts: 0 });
const tip = ref<{ text: string; bad: boolean }>({ text: "", bad: false });
const measureResult = ref<{ text: string; bad: boolean }>({ text: "", bad: false });
const coolUntil = ref(0);
const nowTick = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;

const dirty = computed(() => Object.keys(quotaBody(snap.value, form.value)).length > 0);
const coolLeft = computed(() => {
  const left = (coolUntil.value - nowTick.value) / 1000;
  return left > 0 ? left : 0;
});
const measureDisabled = computed(() => !props.isMaster || coolLeft.value > 0);
const adviceText = computed(() => (props.advice ? adviceLine(props.advice) : "尚未读取执行体数据"));
const adviceNote = computed(() => {
  const rec = (props.advice && (props.advice.recommendation as Record<string, unknown>)) || null;
  return rec && rec.note ? String(rec.note) : "";
});

watch(
  () => props.settings,
  (v) => {
    snap.value = quotaSnapshot(v || {});
    form.value = { users: snap.value.users, accounts: snap.value.accounts };
    tip.value = { text: "", bad: false };
  },
  { immediate: true },
);

function ops(): Record<string, (c: unknown, a?: unknown) => Promise<boolean>> | undefined {
  return (window as { YB?: { settingsOps?: Record<string, (c: unknown, a?: unknown) => Promise<boolean>> } }).YB?.settingsOps;
}
async function save(): Promise<boolean> {
  const o = ops();
  if (!o) return false;
  const body = quotaBody(snap.value, form.value);
  if (!Object.keys(body).length) {
    tip.value = { text: "没有需要保存的改动", bad: false };
    return true;
  }
  const ok = await o.quotaSave(
    { isMaster: props.isMaster, tip: (t: string, b: boolean) => (tip.value = { text: t, bad: b }), onSaved: props.onSaved },
    body,
  );
  if (ok) snap.value = { users: form.value.users, accounts: form.value.accounts };
  return ok;
}

const baseResult = ref("");
const baseBad = ref(false);
// 冷却/在途：结果行**追加**剩余时间（"点了没反应"变成"还要等多久"）。追加而不是替换：
// 实测结果本身就是要看的内容，倒计时只是补充说明。
function paintMeasure(): void {
  if (coolLeft.value > 0 && !timer) {
    timer = setInterval(() => {
      nowTick.value = Date.now();
      paintMeasure();
      if (coolLeft.value <= 0 && timer) { clearInterval(timer); timer = null; }
    }, 1000);
  }
  const suffix = coolLeft.value > 0
    ? (baseResult.value ? "　" : "") + "冷却中：还需 " + clockText(coolLeft.value)
      + " 才能再测（后端全局冷却，连点只会产生一次真实登录）。"
    : "";
  measureResult.value = { text: baseResult.value + suffix, bad: baseBad.value };
}
function measure(): void {
  const o = ops();
  if (!o || measureDisabled.value) return;
  void o.quotaMeasure({
    isMaster: props.isMaster,
    onMeasure: (d: Record<string, unknown> | null, err: unknown) => {
      if (d) {
        const sec = d.seconds != null ? Number(d.seconds) : null;
        const cap = Number(d.per_executor_capacity) || 0;
        const per = Number(d.recommended_per_executor) || 0;
        baseResult.value = "实测 " + (sec == null ? "—" : sec.toFixed(2) + " 秒")
          + "（样本 " + String(d.sample ?? "") + "）：单执行体容量约 " + cap + " 个、建议每执行体 " + per + " 个账号。"
          + "这是窗口外粗量（只覆盖登录 + 拉任务，比真实签到偏乐观）且不会自动保存。";
        baseBad.value = false;
        const cd = Number(d.cooldown_sec) || 0;
        if (cd > 0) coolUntil.value = Date.now() + cd * 1000;
      } else {
        const e = err as { data?: { next_allowed_in?: number }; message?: string };
        const left = e && e.data ? Number(e.data.next_allowed_in) : 0;
        if (left > 0) coolUntil.value = Date.now() + left * 1000;
        baseResult.value = (e && e.message) || "实测失败，请稍后重试";
        baseBad.value = true;
      }
      nowTick.value = Date.now();
      paintMeasure();
    },
  });
}

onUnmounted(() => { if (timer) clearInterval(timer); });
defineExpose({ isDirty: () => dirty.value, save });
</script>

<template>
  <section class="card" id="set-quota">
    <div class="panel-head">
      <div class="panel-head-row">
        <h2 class="panel-title">容量配额</h2>
        <div class="set-head-actions">
          <button type="button" class="info-tip" aria-label="容量上限口径" aria-describedby="set-pop-cap-scope">
            <svg aria-hidden="true"><use href="#i-info" /></svg>
            <span class="info-pop" id="set-pop-cap-scope" role="tooltip"><b>本页上限管"名额"：</b>能存多少用户与账号（0 = 不限），调小只挡新增、不删存量。<b>「数据总览」的容量估算</b>按有效窗口与账号间隔算"一轮装不装得下"（服务能力），<b>下一张卡的"建议"</b>另含慢账号余量（实测容量 × 2/3）。三者口径不同，<b>数值不必相等</b>，不要拿一个去校准另一个。</span>
          </button>
          <span class="badge badge--warn" id="set-cap-dirty" :hidden="!dirty || !isMaster">有未保存的修改</span>
        </div>
      </div>
    </div>
    <p class="alert info" id="set-cap-perm" :hidden="isMaster">
      <span class="ico"><svg aria-hidden="true"><use href="#i-info" /></svg></span>
      <span class="body">仅主管理员可修改容量上限，控件已禁用；需调整请联系主管理员。</span>
    </p>
    <div class="form-grid">
      <div class="field field--narrow">
        <label class="field-label" for="set-max-users">用户容量上限</label>
        <div class="input-group">
          <input id="set-max-users" v-model.number="form.users" class="input" type="number" min="0" max="100000" step="1" placeholder="0" :disabled="!isMaster" />
          <span class="addon">人</span>
        </div>
        <p class="field-help">管归属注册名额；0 = 不限。调小只限制新增，不删存量用户。</p>
      </div>
      <div class="field field--narrow">
        <label class="field-label" for="set-max-accounts">账号容量上限</label>
        <div class="input-group">
          <input id="set-max-accounts" v-model.number="form.accounts" class="input" type="number" min="0" max="100000" step="1" placeholder="0" :disabled="!isMaster" />
          <span class="addon">个</span>
        </div>
        <p class="field-help">管账号存量（含主管理员直属裸账号）；0 = 不限。调小只限制新增，不删存量账号。</p>
      </div>
    </div>
    <p class="set-tip" :class="{ 'set-bad': tip.bad }" id="set-cap-tip" role="status">{{ tip.text }}</p>
    <div class="form-actions">
      <button type="button" class="btn btn--primary btn--sm" id="set-cap-save" :hidden="!dirty || !isMaster" @click="save()">保存容量上限</button>
    </div>
  </section>

  <section v-if="isMaster" class="card" id="set-cap-advice">
    <div class="panel-head">
      <div class="panel-head-row">
        <h2 class="panel-title">容量建议与耗时实测</h2>
        <div class="set-head-actions">
          <button type="button" class="btn btn--ghost btn--sm" id="set-cap-measure" :disabled="measureDisabled" :title="coolLeft > 0 ? '冷却中，还需 ' + clockText(coolLeft) : undefined" @click="measure">测试单账号耗时</button>
          <button type="button" class="btn btn--ghost btn--sm" data-doc="set-pop-cap-advice" data-doc-title="实测说明与建议口径">
            <svg aria-hidden="true"><use href="#i-info" /></svg>口径说明
          </button>
          <div id="set-pop-cap-advice" hidden><b>建议怎么来的：</b>每个执行体的建议账号数＝实测容量 × 2/3（含慢账号余量），建议并行执行体数＝⌈计入容量的账号数 ÷ 每个执行体的建议账号数⌉，只数清单里的「并行」行；<b>是建议不是程序上限</b>（执行体清单最多 64 行，与它无关），也不自动改配置——要改上限请用上方的容量配额，要改执行体请到「执行体」分区。<b>测试做什么：</b>用一个<b>真实账号</b>（默认取账号列表里第一个可签账号，且顺序稳定、每次都是同一个）走一次只读链路（登录 + 拉任务，<b>不提交签到</b>）测出单账号耗时并据此换算容量。它只覆盖窗口外的"登录 + 拉任务"（5 次请求），比真实签到（含定位与提交，6 次请求）偏乐观，别和测试机基准的数混用；结果<b>不会自动保存</b>，本卡只负责展示；正式定档由部署者按测试机基准写入配置文件的实测容量项（页面上没有该录入控件），写入后长期保留，直到按下一次实测的结果更新。<b>限制：</b>仅主管理员可用（不改配置，故与手动签到同档、不收管理员口令，但要按一次确认）；两次实测之间有冷却（默认 10 分钟），冷却期内按钮灰掉并显示剩余时间；落在有效签到窗口内不做（避免抢占本轮资源）。</div>
        </div>
      </div>
    </div>
    <p class="set-summary" id="set-cap-advice-line">{{ adviceText }}</p>
    <p class="set-warn" id="set-cap-advice-note" :hidden="!adviceNote">{{ adviceNote }}</p>
    <p class="set-tip" :class="{ 'set-bad': measureResult.bad }" id="set-cap-measure-result" role="status">{{ measureResult.text }}</p>
  </section>
</template>
