<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { api } from "../lib/shell";
import {
  FOCUS_TARGET,
  drawerForm,
  eventsQuery,
  type DrawerForm,
  type RowView,
} from "./model";

/**
 * 对象抽屉（lcpx 试点，2026-10-25）：详情 + 近 30 天事件 + 操作。
 *
 * 复用面：队列页先用；台账页将来复用。组件只做**只读取数与渲染**，
 * 写操作经 `action` 事件交回宿主页执行（确认桥 / 刷新 / 提示都是页面职责）。
 *
 * 两形态（model.ts 的 drawerForm）：≤720px 全屏（size 100%）、宽屏侧滑 480px
 * 覆盖式——不引入内容列 max-width。初始焦点落**关闭钮**（FOCUS_TARGET），
 * 与 ep-confirm「焦点落取消侧」同一安全口径：破坏性动作不因回车直发。
 *
 * 脱敏纪律：详情接口（/api/accounts/<idx>/detail）会回传完整手机号与完整邮箱，
 * 本组件只渲染服务端遮罩字段（phone_code_masked / owner_display / target.phone）；
 * 明文字段留在内存，绝不进 DOM。e2e 以「完整号不出现在页面可见文本」钉住。
 */

interface DrawerDetail {
  account?: {
    phone?: string;
    phone_model?: string;
    phone_code_masked?: string;
    has_phone_code?: boolean;
    has_password?: boolean;
    owner?: string;
    owner_display?: string;
    status?: string;
    reject_reason?: string;
    user_paused?: boolean;
  };
}

interface EventItem {
  ts: string;
  status: string;
  message: string;
  stage: string;
}

const props = defineProps<{ target: RowView | null }>();
const emit = defineEmits<{
  (e: "close"): void;
  (e: "action", payload: { key: "approve" | "reject" | "edit" | "remove"; row: RowView }): void;
}>();

const open = ref(false);
const form = ref<DrawerForm>(drawerForm(window.innerWidth));
const detail = ref<DrawerDetail | null>(null);
const events = ref<EventItem[]>([]);
const eventsCount = ref(0);
const eventsTruncated = ref(false);
const loading = ref(false);
const errorText = ref("");

const canReview = computed(() => {
  const st = props.target?.status ?? "";
  return st === "pending" || st === "rejected";
});

function onResize(): void {
  form.value = drawerForm(window.innerWidth);
}

/** 初始焦点送关闭钮：EP 在自己的帧里挂抽屉，按帧轮询直到出现（同 ep-confirm 口径）。 */
function focusClose(): void {
  let tries = 0;
  const step = (): void => {
    if (tries++ > 10) return;
    const btn = document.querySelector<HTMLElement>(FOCUS_TARGET);
    if (btn) {
      btn.focus();
      return;
    }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

async function load(row: RowView): Promise<void> {
  loading.value = true;
  errorText.value = "";
  detail.value = null;
  events.value = [];
  eventsCount.value = 0;
  eventsTruncated.value = false;
  try {
    const [d, ev] = await Promise.all([
      api<DrawerDetail>("GET", `/api/accounts/${row.index}/detail`),
      api<{ events?: EventItem[]; count?: number; truncated?: boolean }>(
        "GET", eventsQuery(row.index),
      ),
    ]);
    detail.value = d;
    events.value = ev.events ?? [];
    eventsCount.value = Number(ev.count) || events.value.length;
    eventsTruncated.value = !!ev.truncated;
  } catch (err) {
    errorText.value = (err as { message?: string })?.message || "加载失败，请稍后再试";
  } finally {
    loading.value = false;
  }
}

watch(
  () => props.target,
  (t, prev) => {
    if (t && t !== prev) {
      open.value = true;
      void load(t);
      focusClose();
      return;
    }
    // 宿主清空目标（动作成功后刷新）⇒ 收抽屉；用户自己关（open→false）已发 close。
    if (!t && prev) open.value = false;
  },
);

watch(open, (v) => {
  if (!v) emit("close");
});

onMounted(() => window.addEventListener("resize", onResize));
onBeforeUnmount(() => window.removeEventListener("resize", onResize));

function act(key: "approve" | "reject" | "edit" | "remove"): void {
  if (props.target) emit("action", { key, row: props.target });
}
</script>

<template>
  <el-drawer
    v-model="open"
    :size="form.size"
    :class="form.cls"
    :with-header="true"
    :append-to-body="true"
    :destroy-on-close="true"
    class="obj-drawer"
  >
    <template #header>
      <div class="obj-drawer-head">
        <span class="obj-drawer-title" data-drawer-title>{{ target?.name || "详情" }}</span>
        <span class="obj-drawer-badge">{{ target?.badgeText }}</span>
      </div>
    </template>

    <el-alert v-if="errorText" type="error" :title="errorText" :closable="false" show-icon />
    <el-skeleton v-if="loading && !detail" :rows="4" animated />

    <template v-else-if="target">
      <section class="obj-drawer-sec">
        <h3 class="obj-drawer-sec-title">详情</h3>
        <dl class="obj-drawer-facts">
          <div class="obj-fact"><dt>手机号</dt><dd class="mono">{{ target.phone }}</dd></div>
          <div class="obj-fact"><dt>机型</dt><dd>{{ detail?.account?.phone_model || target.model || "—" }}</dd></div>
          <div class="obj-fact">
            <dt>识别码</dt>
            <dd class="mono">{{ detail?.account?.has_phone_code ? detail?.account?.phone_code_masked : "—" }}</dd>
          </div>
          <div class="obj-fact"><dt>归属</dt><dd>{{ detail?.account?.owner_display || target.owner || "—" }}</dd></div>
          <div class="obj-fact">
            <dt>今日计划</dt>
            <dd>{{ target.planAt ? `计划 ${target.planAt}` : (target.stateMsg || "—") }}</dd>
          </div>
        </dl>
      </section>

      <section class="obj-drawer-sec">
        <h3 class="obj-drawer-sec-title">
          近 30 天事件
          <span class="obj-drawer-sec-meta">共 {{ eventsCount }} 条<template v-if="eventsTruncated">（已截断）</template></span>
        </h3>
        <div class="obj-drawer-events" data-drawer-events>
          <p v-if="!events.length" class="obj-drawer-empty">窗口内没有事件记录</p>
          <ul v-else class="obj-event-list">
            <li v-for="(ev, i) in events" :key="i" class="obj-event">
              <span class="obj-event-ts mono">{{ ev.ts }}</span>
              <span class="obj-event-status">{{ ev.status }}</span>
              <span class="obj-event-msg">{{ ev.message }}</span>
            </li>
          </ul>
        </div>
        <a class="obj-drawer-link" href="/data/logs">看全部 → 台账</a>
      </section>

      <section class="obj-drawer-sec obj-drawer-actions">
        <el-button
          v-if="canReview"
          type="primary"
          size="small"
          data-drawer-action="approve"
          @click="act('approve')"
        >通过</el-button>
        <el-button v-if="canReview" size="small" data-drawer-action="reject" @click="act('reject')">驳回</el-button>
        <el-button size="small" data-drawer-action="edit" @click="act('edit')">编辑</el-button>
        <el-button size="small" type="danger" plain data-drawer-action="remove" @click="act('remove')">删除</el-button>
      </section>
    </template>
  </el-drawer>
</template>

<style scoped>
/* 样式只消费既有令牌（Adminator token 经 app.css 全局下发），本组件不定义新令牌。
   间距取 4-20 紧凑档；组标题用 nav-label 式小字（大写间距不适用于中文，取小字弱色）。 */
.obj-drawer-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.obj-drawer-title {
  font-weight: 700;
  color: var(--t-base);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.obj-drawer-badge {
  flex: none;
  font-size: 12px;
  color: var(--t-muted);
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  padding: 1px 8px;
}
.obj-drawer-sec {
  margin-bottom: 16px;
}
.obj-drawer-sec-title {
  margin: 0 0 8px;
  font-size: var(--fs-label);
  font-weight: 600;
  color: var(--t-muted);
}
.obj-drawer-sec-meta {
  margin-left: 6px;
  font-weight: 400;
}
.obj-drawer-facts {
  margin: 0;
  display: grid;
  gap: 4px;
}
.obj-fact {
  display: flex;
  gap: 10px;
  font-size: 13px;
  line-height: 1.6;
}
.obj-fact dt {
  flex: none;
  width: 56px;
  color: var(--t-muted);
}
.obj-fact dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--t-base);
}
.obj-drawer-events {
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  padding: 8px 12px;
  max-height: 320px;
  overflow: auto;
}
.obj-event-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.obj-event {
  display: flex;
  gap: 8px;
  padding: 4px 0;
  font-size: 12px;
  line-height: 1.5;
  border-bottom: 1px solid var(--border-soft);
}
.obj-event:last-child {
  border-bottom: 0;
}
.obj-event-ts {
  flex: none;
  color: var(--t-muted);
  font-variant-numeric: tabular-nums;
}
.obj-event-status {
  flex: none;
  color: var(--t-sub);
}
.obj-event-msg {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--t-base);
}
.obj-drawer-empty {
  margin: 6px 0;
  font-size: 12px;
  color: var(--t-muted);
}
.obj-drawer-link {
  display: inline-block;
  margin-top: 8px;
  font-size: 12px;
  color: var(--primary);
}
.obj-drawer-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.mono {
  font-family: var(--font-mono);
  font-size: 12px;
}
</style>
