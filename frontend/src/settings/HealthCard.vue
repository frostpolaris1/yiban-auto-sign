<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { HEALTH_REPORT_WEEKDAYS, healthBody, healthSnapshot } from "./model.js";

/* 健康与探针分区（任意管理员可见；写仅主管理员）。
   legacy = components/settings-health.js。探针/账号验证会对全站账号做真实登录，故 A 档。
   告警通道健康报告的发送时刻同属 A 档：把时刻改到没人看的钟点等于静默失去周期性证据。 */

const props = defineProps<{ settings: Record<string, unknown>; isMaster: boolean }>();

const snap = ref(healthSnapshot({}));
const form = ref({
  verify: false, probe: false, time: "20:00", interval: "1",
  reportFixed: false, reportTime: "09:00", reportWeekday: "0",
});
//: 用户把开关打开、而时刻还空着时补一个可用的初值。**只在开的那一下补**，不在初始化时补：
//: .env 里只配了星期（没配时刻）时，初始化就补 09:00 会让面板一打开就带脏标记，
//: 随手一保存就把"那天唤醒即发"悄悄改成"09:00 发"（打开一次就改了行为）。
const REPORT_TIME_ON_ENABLE = "09:00";
//: 星期下拉的显示缺省（未配置时的行为回落就是周一）。只作显示，不进快照——
//: 快照用空串表示"没配"，界面才分辨得出"配了周一"与"没配"。
const REPORT_WEEKDAY_FALLBACK = "0";
const tip = ref<{ text: string; bad: boolean }>({ text: "", bad: false });

const intervalItems = [
  { value: "1", label: "每天" },
  { value: "2", label: "每 2 天" },
  { value: "3", label: "每 3 天" },
  { value: "7", label: "每 7 天" },
  { value: "once", label: "下次计划时间单次执行（执行后自动关闭）" },
];
const weekdayItems = HEALTH_REPORT_WEEKDAYS;

const dirty = computed(() => Object.keys(healthBody(snap.value, form.value)).length > 0);

watch(
  () => props.settings,
  (v) => {
    snap.value = healthSnapshot(v || {});
    form.value = {
      verify: !!snap.value.verify,
      probe: !!snap.value.probe,
      time: snap.value.time,
      interval: snap.value.interval,
      // 开关状态由"这一对键有没有配过"派生（时刻或星期任一存在）：只按时刻派生时，
      // 手工写进 .env 的"只有星期"会让面板显示"已关闭"而报告其实按那个星期发。
      reportFixed: !!(snap.value.reportTime || snap.value.reportWeekday),
      // 时刻不补显示缺省：未配时刻就留空（下拉框留空）。在这里补缺省就会让"只配了星期"
      // 的存量配置一打开就带假脏标记、一保存就被改写成"09:00 发"。
      reportTime: snap.value.reportTime,
      reportWeekday: snap.value.reportWeekday || REPORT_WEEKDAY_FALLBACK,
    };
    tip.value = { text: "", bad: false };
  },
  { immediate: true },
);

/* 开关打开时补时刻初值，**只挂在开关的 change 事件上**（用户交互那一下）。
   刻意不用 `watch(() => form.reportFixed)`：设置是异步到来的（props.settings 先是 `{}`），
   等真设置回来时 reportFixed 会从 false 变 true，watch 那一下补初值就等于"打开设置页"
   这个动作本身把"只配了星期"的存量配置改写成"09:00 发"，并留下一条假脏标记。 */
function onReportFixedToggle(on: unknown): void {
  if (on && !form.value.reportTime) form.value.reportTime = REPORT_TIME_ON_ENABLE;
}

function ops(): Record<string, (c: unknown, a?: unknown) => Promise<boolean>> | undefined {
  return (window as { YB?: { settingsOps?: Record<string, (c: unknown, a?: unknown) => Promise<boolean>> } }).YB?.settingsOps;
}
async function save(): Promise<boolean> {
  const o = ops();
  if (!o) return false;
  const body = healthBody(snap.value, form.value);
  if (!Object.keys(body).length) {
    tip.value = { text: "没有需要保存的改动", bad: false };
    return true;
  }
  const ok = await o.healthSave(
    { isMaster: props.isMaster, tip: (t: string, b: boolean) => (tip.value = { text: t, bad: b }) },
    body,
  );
  if (ok) {
    // 本地快照按**刚落盘的那一份**推进：只把提交过的字段替换成提交值，没提交的保持原样。
    // 一律按表单当前值重建会让"没提交的字段"在本地与服务端分叉（例如只配了星期时
    // 表单显示周一、服务端仍是没有这个键），于是下次打开又冒出一条假脏标记。
    snap.value = healthSnapshot({
      account_verify: form.value.verify ? 1 : 0,
      probe_enable: form.value.probe ? 1 : 0,
      probe_time: form.value.time,
      probe_interval: form.value.interval,
      health_report_time: "health_report_time" in body
        ? body.health_report_time : snap.value.reportTime,
      health_report_weekday: "health_report_weekday" in body
        ? body.health_report_weekday : snap.value.reportWeekday,
    });
  }
  return ok;
}
// 时间控件的回写是同一件事（砍到 HH:MM），两个字段共用一处——各写一份必然漂移。
// 返回 null = "这次更新不表达任何意图"（控件被清空）：时间控件是单向绑定，清空无法表达
// "把这项设置清掉"（那要关掉上面的开关），故忽略清空、保留上一个有效值。
function sliceHhmm(v: unknown): string | null {
  return typeof v === "string" && v ? v.slice(0, 5) : null;
}
function onTimeChange(v: unknown): void {
  const t = sliceHhmm(v);
  if (t) form.value.time = t;
}
function onReportTimeChange(v: unknown): void {
  const t = sliceHhmm(v);
  if (t) form.value.reportTime = t;
}

defineExpose({ isDirty: () => dirty.value, save });
</script>

<template>
  <section class="card" id="set-health">
    <div class="panel-head">
      <div class="panel-head-row">
        <h2 class="panel-title">健康与探针</h2>
        <div class="set-head-actions">
          <span class="badge badge--warn" id="sh-dirty" :hidden="!dirty || !isMaster">有未保存的修改</span>
        </div>
      </div>
    </div>
    <div class="set-row">
      <label class="set-row-text" for="sh-verify">
        <span class="set-row-label">注册时验证账号</span>
        <p class="set-help">开启后提交账号会即时验证，无法通过将当场提示。</p>
      </label>
      <el-switch id="sh-verify" v-model="form.verify" aria-label="注册时验证账号" :disabled="!isMaster" />
    </div>
    <div class="set-row">
      <label class="set-row-text" for="sh-probe-enable">
        <span class="set-row-label">开启探针模式</span>
        <p class="set-help">非签到时段对全部账号做健康检查（仅登录，不实际签到），异常账号提前预警。</p>
      </label>
      <el-switch id="sh-probe-enable" v-model="form.probe" aria-label="探针模式" :disabled="!isMaster" />
    </div>
    <p class="alert info" id="sh-perm" :hidden="isMaster">仅主管理员可修改健康与探针设置。</p>
    <div class="form-grid">
      <div class="field">
        <span class="field-label" id="sh-probe-time-label">触发时间（最早时刻）</span>
        <el-time-picker
          id="sh-probe-time"
          :model-value="form.time"
          format="HH:mm"
          value-format="HH:mm"
          aria-label="触发时间"
          :disabled="!isMaster"
          style="width: 100%"
          @update:model-value="onTimeChange"
        />
        <p class="field-help">到达设定时间后，将在最近的调度周期执行（约 10 分钟内）；每天最多一次。</p>
      </div>
      <div class="field">
        <span class="field-label" id="sh-probe-interval-label">触发频率</span>
        <div class="select-field" data-select-field="sh-probe-interval">
          <el-select id="sh-probe-interval" v-model="form.interval" aria-label="触发频率" :disabled="!isMaster" style="width: 100%">
            <el-option v-for="it in intervalItems" :key="it.value" :value="it.value" :label="it.label" />
          </el-select>
        </div>
      </div>
    </div>

    <div class="set-row">
      <label class="set-row-text" for="sh-report-fixed">
        <span class="set-row-label">按固定时刻发送健康报告</span>
        <p class="set-help">告警通道健康报告（例行每周一封，通道降级或当日推送额度耗尽时当天加发）目前跟着部署节拍到达：服务重启后到达时间会跟着漂移，深夜部署就在半夜收到。开启后按下面的时刻与星期发出；关闭则回到现状（周一 + 服务启动时的钟点），并把已配的时刻与星期一并清除。</p>
      </label>
      <el-switch id="sh-report-fixed" v-model="form.reportFixed" aria-label="按固定时刻发送健康报告" :disabled="!isMaster" @change="onReportFixedToggle" />
    </div>
    <div class="form-grid">
      <div class="field">
        <span class="field-label" id="sh-report-time-label">报告发送时刻</span>
        <el-time-picker
          id="sh-report-time"
          :model-value="form.reportTime"
          format="HH:mm"
          value-format="HH:mm"
          aria-label="报告发送时刻"
          :disabled="!isMaster || !form.reportFixed"
          style="width: 100%"
          @update:model-value="onReportTimeChange"
        />
        <p class="field-help">到达该时刻后的最近一次清理线程唤醒发出，误差不超过 5 分钟（时刻落在跨零点那一跳的空隙里时，零点后的第一跳补发：当周可能多一封，不会丢）。时刻按北京时间（UTC+8，与签到窗口同一时基）判定。改完下一轮生效（约 5 分钟内），不必重启；通道降级与额度耗尽的当天加发不受本设置影响。</p>
      </div>
      <div class="field">
        <span class="field-label" id="sh-report-weekday-label">报告发送星期</span>
        <div class="select-field" data-select-field="sh-report-weekday">
          <el-select id="sh-report-weekday" v-model="form.reportWeekday" aria-label="报告发送星期" :disabled="!isMaster || !form.reportFixed" style="width: 100%">
            <el-option v-for="it in weekdayItems" :key="it.value" :value="it.value" :label="it.label" />
          </el-select>
        </div>
        <p class="field-help">与上面的时刻同属一项设置，开关关闭时一并清除（时刻留空也不影响：只填星期同样有效，那一天按服务启动钟点发）。缺省周一。</p>
      </div>
    </div>
    <p class="set-tip" :class="{ 'set-bad': tip.bad }" id="sh-tip" role="status">{{ tip.text }}</p>
    <div class="form-actions">
      <button type="button" class="btn btn--primary btn--sm" id="sh-save" :disabled="!dirty || !isMaster" @click="save()">保存探针设置</button>
    </div>
  </section>
</template>
