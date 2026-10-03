<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { annDraftMetaText, annMeta, annPublishState, sanitizeAnnouncement } from "./model.js";

/* 全局公告卡（双人发布：任意管理员写草稿，主管理员发布/下线）。
   legacy = pages/work_settings.js 的公告段。草稿任意管理员可编辑保存；发布/下线仅主管理员
   且需当次口令（走受门禁 helper）。后端禁换行：前端在输入阶段就把换行族换成空格。 */

const props = defineProps<{
  isMaster: boolean;
  announcement: Record<string, unknown>;
  onReload: () => Promise<void>;
}>();
const emit = defineEmits<{ (e: "dirty-change"): void }>();

const draft = ref("");
const draftBy = ref("");
const draftAt = ref("");
const online = ref("");
const publishedBy = ref("");
const publishedAt = ref("");
const dirty = ref(false);
const tip = ref<{ text: string; bad: boolean }>({ text: "", bad: false });

function applyRemote(d: Record<string, unknown>): void {
  draftBy.value = String(d.draft_by || "");
  draftAt.value = String(d.draft_at || "");
  online.value = String(d.text || "");
  publishedBy.value = String(d.published_by || "");
  publishedAt.value = String(d.published_at || "");
  draft.value = String(d.draft || "");
  dirty.value = false;
  tip.value = { text: "", bad: false };
  emit("dirty-change");
}
watch(() => props.announcement, (v) => { if (v) applyRemote(v); }, { immediate: true });

const draftMeta = computed(() => annDraftMetaText(draft.value, draftBy.value, draftAt.value));
const onlineText = computed(() => online.value || "（当前没有线上公告）");
const onlineMeta = computed(() => (online.value ? annMeta(publishedBy.value, publishedAt.value) : ""));
const pubState = computed(() => annPublishState(draft.value, online.value));

function onInput(): void {
  const v = sanitizeAnnouncement(draft.value);
  if (v !== draft.value) draft.value = v;
  dirty.value = v.trim() !== String(props.announcement.draft || "");
  emit("dirty-change");
}

function ops(): Record<string, (c: unknown, a?: unknown) => Promise<boolean>> | undefined {
  return (window as { YB?: { settingsOps?: Record<string, (c: unknown, a?: unknown) => Promise<boolean>> } }).YB?.settingsOps;
}
function ctx(): Record<string, unknown> {
  return {
    isMaster: props.isMaster,
    load: () => props.onReload(),
    tip: (t: string, b: boolean) => (tip.value = { text: t, bad: b }),
  };
}

async function save(): Promise<boolean> {
  const o = ops();
  if (!o) return false;
  return o.announcementSaveDraft(ctx(), draft.value);
}
function publish(): void {
  const o = ops();
  if (!o || !props.isMaster || pubState.value.noop) return;
  void o.announcementPublish(ctx(), {
    offline: pubState.value.offline,
    draftText: draft.value,
    dirty: dirty.value,
  });
}
function clear(): void {
  const o = ops();
  if (!o) return;
  void o.announcementClear(ctx());
}

defineExpose({ isDirty: () => dirty.value, save, applyRemote });
</script>

<template>
  <section class="card" id="set-announcement-card">
    <div class="panel-head">
      <div class="panel-head-row">
        <h2 class="panel-title">全局公告</h2>
        <div class="set-head-actions">
          <span class="badge badge--warn" id="set-ann-dirty" :hidden="!dirty">有未保存的修改</span>
        </div>
      </div>
    </div>
    <p class="alert info" id="set-ann-perm" :hidden="isMaster">
      <span class="ico"><svg aria-hidden="true"><use href="#i-info" /></svg></span>
      <span class="body">编辑并保存草稿任意管理员均可；发布或下线线上公告仅主管理员可做（需当次口令）。</span>
    </p>
    <div class="field">
      <label class="field-label" for="set-announcement">待发布草稿（保存后不会立即对外显示）</label>
      <textarea id="set-announcement" v-model="draft" class="textarea" rows="2" maxlength="200" placeholder="例如：今晚 22:00–23:00 例行维护，签到可能延迟" @input="onInput" />
      <p class="field-help" id="set-ann-draft-meta">{{ draftMeta }}</p>
      <p class="field-help">限 200 字、禁止换行；发布后显示在所有页面顶部（含登录页）。</p>
    </div>
    <div class="ann-published">
      <span class="field-label">线上公告（当前对所有访问者可见）</span>
      <p class="set-summary" id="set-ann-published">{{ onlineText }}</p>
      <p class="field-help" id="set-ann-published-meta">{{ onlineMeta }}</p>
    </div>
    <p class="set-tip" :class="{ 'set-bad': tip.bad }" id="set-ann-tip" role="status">{{ tip.text }}</p>
    <div class="form-actions">
      <button type="button" class="btn btn--primary btn--sm" id="set-ann-save" :hidden="!dirty" @click="save()">保存草稿</button>
      <button type="button" class="btn btn--ghost btn--sm" id="set-ann-clear" @click="clear">清除草稿</button>
      <span class="spacer" />
      <button
        type="button"
        class="btn btn--primary btn--sm"
        id="set-ann-publish"
        :disabled="!isMaster || pubState.noop"
        :title="!isMaster ? '发布/下线线上公告仅主管理员可做' : (pubState.noop ? '草稿与线上公告都为空，没有可执行的操作' : undefined)"
        @click="publish"
      >{{ pubState.offline ? "下线线上公告" : "发布公告" }}</button>
    </div>
  </section>
</template>
