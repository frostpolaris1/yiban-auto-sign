<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { healthBody, healthSnapshot } from "./model.js";

/* 健康与探针分区（任意管理员可见；写仅主管理员）。
   legacy = components/settings-health.js。探针/账号验证会对全站账号做真实登录，故 A 档。 */

const props = defineProps<{ settings: Record<string, unknown>; isMaster: boolean }>();

const snap = ref(healthSnapshot({}));
const form = ref({ verify: false, probe: false, time: "20:00", interval: "1" });
const tip = ref<{ text: string; bad: boolean }>({ text: "", bad: false });

const intervalItems = [
  { value: "1", label: "每天" },
  { value: "2", label: "每 2 天" },
  { value: "3", label: "每 3 天" },
  { value: "7", label: "每 7 天" },
  { value: "once", label: "下次计划时间单次执行（执行后自动关闭）" },
];

const dirty = computed(() => Object.keys(healthBody(snap.value, form.value)).length > 0);

watch(
  () => props.settings,
  (v) => {
    snap.value = healthSnapshot(v || {});
    form.value = { verify: !!snap.value.verify, probe: !!snap.value.probe, time: snap.value.time, interval: snap.value.interval };
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
  const body = healthBody(snap.value, form.value);
  if (!Object.keys(body).length) {
    tip.value = { text: "没有需要保存的改动", bad: false };
    return true;
  }
  const ok = await o.healthSave(
    { isMaster: props.isMaster, tip: (t: string, b: boolean) => (tip.value = { text: t, bad: b }) },
    body,
  );
  if (ok) snap.value = healthSnapshot({ account_verify: form.value.verify ? 1 : 0, probe_enable: form.value.probe ? 1 : 0, probe_time: form.value.time, probe_interval: form.value.interval });
  return ok;
}
function onTimeChange(v: unknown): void {
  if (typeof v === "string" && v) form.value.time = v.slice(0, 5);
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
    <p class="set-tip" :class="{ 'set-bad': tip.bad }" id="sh-tip" role="status">{{ tip.text }}</p>
    <div class="form-actions">
      <button type="button" class="btn btn--primary btn--sm" id="sh-save" :disabled="!dirty || !isMaster" @click="save()">保存探针设置</button>
    </div>
  </section>
</template>
