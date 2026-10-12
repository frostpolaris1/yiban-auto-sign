<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { canDo, pauseHintText, whyFor } from "./model.js";

/* 系统开关分区（整 tab 仅主管理员）。
   legacy = components/settings-switches.js。权限按**变更方向**分档：
     急停（global_pause 0→1）任意管理员可做；恢复（1→0）与注册开关两方向仅主管理员。
   所以「暂停」与「恢复」是两颗不同权限的按钮，不是一颗翻转钮。 */

const props = defineProps<{ settings: Record<string, unknown>; isMaster: boolean }>();

const state = ref({ globalPause: false, regPause: false });
watch(
  () => props.settings,
  (v) => { state.value = { globalPause: !!(v && v.global_pause), regPause: !!(v && v.registration_pause) }; },
  { immediate: true },
);

const hint = computed(() => pauseHintText(state.value));
function allowed(field: string, next: boolean): boolean { return canDo(field, next, props.isMaster); }
function why(field: string): string { return whyFor(field); }
const anyDenied = computed(() => {
  const rows: Array<[string, boolean]> = [
    ["global_pause", state.value.globalPause],
    ["registration_pause", state.value.regPause],
  ];
  return rows.some(([field, paused]) => {
    // 每行只露出"当前状态对应的下一步动作"那颗钮；无权限的那颗禁用
    return (paused && !allowed(field, false)) || (!paused && !allowed(field, true));
  });
});

function act(field: string, next: boolean): void {
  if (!allowed(field, next)) return;
  const ops = (window as { YB?: { settingsOps?: Record<string, (c: unknown, ...a: unknown[]) => Promise<boolean>> } }).YB?.settingsOps;
  if (!ops) return;
  void ops.switchesPause({
    isMaster: props.isMaster,
    onPaused: (f: string, n: boolean) => {
      state.value = { ...state.value, [f === "global_pause" ? "globalPause" : "regPause"]: n };
    },
  }, field, next);
}
</script>

<template>
  <section class="card set-danger" id="set-switches">
    <div class="panel-head">
      <div class="panel-head-row">
        <h2 class="panel-title">系统开关</h2>
        <div class="set-head-actions">
          <button type="button" class="info-tip" aria-label="暂停签到与暂停注册的后果" aria-describedby="set-pop-switches">
            <svg aria-hidden="true"><use href="#i-info" /></svg>
            <span class="info-pop" id="set-pop-switches" role="tooltip">暂停签到＝所有账号停止自动签到（<b>从下次开始</b>）；<b>手动签到不受影响</b>，正在跑的这一轮也不受影响。恢复后下一轮定时任务照常。注册暂停只挡新注册，不影响已有账号签到。</span>
          </button>
        </div>
      </div>
    </div>
    <p class="set-danger-note">暂停签到：所有账号停止自动签到（从下次开始），手动签到不受影响。
      暂停注册：登录页关闭注册入口，已注册用户登录不受影响，需在「账号管理」手动添加账号。
      两项都立即生效（不属于表单值，无「保存」概念），均需二次确认 + 当前管理员密码。</p>
    <div class="set-danger-actions">
      <button type="button" class="btn btn--danger-ghost set-danger-btn" id="set-gp-pause" :hidden="state.globalPause" :disabled="!allowed('global_pause', true)" :title="!allowed('global_pause', true) ? why('global_pause') : undefined" @click="act('global_pause', true)">
        <svg aria-hidden="true"><use href="#i-pause" /></svg><span>暂停自动签到</span>
      </button>
      <button type="button" class="btn btn--ghost set-danger-btn" id="set-gp-resume" :hidden="!state.globalPause" :disabled="!allowed('global_pause', false)" :title="!allowed('global_pause', false) ? why('global_pause') : undefined" @click="act('global_pause', false)">
        <svg aria-hidden="true"><use href="#i-play" /></svg><span>恢复自动签到</span>
      </button>
      <button type="button" class="btn btn--danger-ghost set-danger-btn" id="set-rp-pause" :hidden="state.regPause" :disabled="!allowed('registration_pause', true)" :title="!allowed('registration_pause', true) ? why('registration_pause') : undefined" @click="act('registration_pause', true)">
        <svg aria-hidden="true"><use href="#i-pause" /></svg><span>暂停注册</span>
      </button>
      <button type="button" class="btn btn--ghost set-danger-btn" id="set-rp-resume" :hidden="!state.regPause" :disabled="!allowed('registration_pause', false)" :title="!allowed('registration_pause', false) ? why('registration_pause') : undefined" @click="act('registration_pause', false)">
        <svg aria-hidden="true"><use href="#i-play" /></svg><span>开放注册</span>
      </button>
    </div>
    <p class="alert info" id="sw-perm" :hidden="!anyDenied">
      <span class="ico"><svg aria-hidden="true"><use href="#i-info" /></svg></span>
      <span class="body">「恢复自动签到」与「暂停/开放注册」仅主管理员可做；「暂停自动签到」是急停动作，任意管理员均可操作。</span>
    </p>
    <p class="set-warn" id="set-pause-hint" :hidden="!hint">{{ hint }}</p>
  </section>
</template>
