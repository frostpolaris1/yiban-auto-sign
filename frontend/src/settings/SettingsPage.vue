<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { api, errorMessage, openModal, shellBase, toast } from "../lib/shell";
import "./ops.js"; // 注册 window.YB.settingsOps（纯 JS，写链被 Python 守卫真跑/钉点）
import ScheduleCard from "./ScheduleCard.vue";
import AnnouncementCard from "./AnnouncementCard.vue";
import NotifyCard from "./NotifyCard.vue";
import QuotaCard from "./QuotaCard.vue";
import HealthCard from "./HealthCard.vue";
import ExecutorsCard from "./ExecutorsCard.vue";
import SwitchesCard from "./SwitchesCard.vue";

/* 系统设置（管理端 /work/settings）—— P3 整页迁移到 Vue。
   与 legacy 的分工：口径层在 `model.js`（纯函数 + Vitest + Python 守卫真跑），写操作链路在
   `ops.js`（纯 JS + window.YB），本组件只做**编排**：身份判定 → 分区与 ?tab= 深链（自管，
   不用 core.js 的 data-tab-target 契约）→ 拉取 GET /api/settings 回填 → 未保存改动守卫。

   保存语义（全页唯一口径）：每个分区/卡片的字段改动只标脏，由各自的保存按钮提交；页面级
   只做两件事 —— 汇总脏分区、在"要离开这些改动"时问一句。带破坏性的按钮（清空收件人 /
   恢复默认调度 / 暂停签到）不属于表单值，保持即时执行 + 二次确认。

   权限：整 tab 级隐藏（执行体 / 系统开关仅主管理员）由本页处理；字段级禁用由各卡按 isMaster
   处理。UI 禁用不是安全边界，后端仍逐字段判档位（403 / 口令门）。

   安全：全页零 v-html（正文一律插值）；写请求走 shell.api / dangerousSubmit（自带 CSRF）。 */

const ADMIN_TABS = [
  { key: "schedule", title: "签到调度", master: false },
  { key: "announcement", title: "公告", master: false },
  { key: "notify", title: "通知通道", master: false },
  { key: "quota", title: "容量配额", master: false },
  { key: "health", title: "健康与探针", master: false },
  { key: "executors", title: "执行体", master: true },
  { key: "switches", title: "系统开关", master: true },
] as const;

const isMaster = ref(false);
const ready = ref(false);
const tab = ref("schedule");
const settings = ref<Record<string, unknown>>({});
const status = ref<{ tone: string; text: string; retry: boolean } | null>(null);
const announcement = ref<Record<string, unknown>>({});
const capAdvice = ref<Record<string, unknown> | null>(null);

// 子组件 ref（脏汇总 / 保存派发）
const scheduleRef = ref<InstanceType<typeof ScheduleCard> | null>(null);
const annRef = ref<InstanceType<typeof AnnouncementCard> | null>(null);
const notifyRef = ref<InstanceType<typeof NotifyCard> | null>(null);
const quotaRef = ref<InstanceType<typeof QuotaCard> | null>(null);
const healthRef = ref<InstanceType<typeof HealthCard> | null>(null);
const execRef = ref<InstanceType<typeof ExecutorsCard> | null>(null);

const visibleTabs = computed(() => ADMIN_TABS.filter((t) => !t.master || isMaster.value));

interface DirtyStore {
  name: string;
  tab: string;
  isDirty(): boolean;
  save(): Promise<boolean>;
}
function stores(): DirtyStore[] {
  const list: DirtyStore[] = [
    { name: "签到调度", tab: "schedule", isDirty: () => !!scheduleRef.value?.isDirty(), save: () => scheduleRef.value?.save() ?? Promise.resolve(true) },
    { name: "全局公告", tab: "announcement", isDirty: () => !!annRef.value?.isDirty(), save: () => annRef.value?.save() ?? Promise.resolve(true) },
    // 推送与邮件各自入列：NotifyCard 的 isDirty 是二者并集，只改邮件时若合并成一条
    // 「消息推送」会在守卫弹窗里报错名字（用户以为改的是另一个通道）。
    { name: "消息推送", tab: "notify", isDirty: () => !!notifyRef.value?.isPushDirty(), save: () => notifyRef.value?.savePush() ?? Promise.resolve(true) },
    { name: "邮件通知", tab: "notify", isDirty: () => !!notifyRef.value?.isMailDirty(), save: () => notifyRef.value?.saveMail() ?? Promise.resolve(true) },
    { name: "容量配额", tab: "quota", isDirty: () => !!quotaRef.value?.isDirty(), save: () => quotaRef.value?.save() ?? Promise.resolve(true) },
    { name: "健康与探针", tab: "health", isDirty: () => !!healthRef.value?.isDirty(), save: () => healthRef.value?.save() ?? Promise.resolve(true) },
  ];
  return list;
}
function dirtyStores(): DirtyStore[] {
  return stores().filter((s) => s.isDirty());
}
function tabHasDirty(key: string): boolean {
  return dirtyStores().some((s) => s.tab === key);
}

/* ---------------- 数据加载 ---------------- */
async function loadSettings(): Promise<void> {
  status.value = { tone: "info", text: "正在加载设置…", retry: false };
  try {
    const data = (await api<Record<string, unknown>>("GET", "/api/settings")) ?? {};
    settings.value = data;
    status.value = null;
  } catch (e) {
    status.value = { tone: "danger", text: "设置加载失败", retry: true };
    toast().error(errorMessage(e, "设置加载失败，请稍后重试"));
    throw e;
  }
}
async function loadAnnouncement(): Promise<void> {
  try {
    const d = (await api<Record<string, unknown>>("GET", "/api/announcement")) ?? {};
    announcement.value = d;
  } catch {
    /* 公告读取失败不阻塞整页 */
  }
}
async function startLoad(): Promise<void> {
  await loadSettings();
  await loadAnnouncement();
  if (isMaster.value) {
    void notifyRef.value?.load();
    void execRef.value?.load();
  }
}
// 放弃修改 = 重新拉一遍服务端值覆盖本地（比重放每个组件的回滚逻辑更不容易漏）
async function reloadAll(): Promise<void> {
  try {
    await Promise.all([
      loadSettings(),
      loadAnnouncement(),
      isMaster.value ? notifyRef.value?.load() : Promise.resolve(),
      isMaster.value ? execRef.value?.load() : Promise.resolve(),
    ]);
  } catch {
    /* 单卡失败不阻塞 */
  }
}

/* ---------------- 分区与深链 ---------------- */
function syncTabUrl(next: string): void {
  try {
    const params = new URLSearchParams(location.search);
    params.set("tab", next);
    history.replaceState(null, "", `${location.pathname}?${params.toString()}${location.hash}`);
  } catch {
    /* 无 history 环境静默降级 */
  }
}
function applyTab(next: string, writeUrl: boolean): void {
  if (!visibleTabs.value.some((t) => t.key === next)) return;
  tab.value = next;
  if (writeUrl) syncTabUrl(next);
}
function selectTab(next: string): void {
  guardThen(() => applyTab(next, true), true);
}
function onTabKeydown(e: KeyboardEvent, key: string): void {
  const keys = visibleTabs.value.map((t) => t.key);
  const i = keys.indexOf(key);
  if (i < 0) return;
  let next = -1;
  if (e.key === "ArrowRight") next = (i + 1) % keys.length;
  else if (e.key === "ArrowLeft") next = (i - 1 + keys.length) % keys.length;
  else if (e.key === "Home") next = 0;
  else if (e.key === "End") next = keys.length - 1;
  else return;
  e.preventDefault();
  const target = keys[next];
  guardThen(() => {
    applyTab(target, true);
    document.getElementById("set-tab-" + target)?.focus();
  }, true);
}

/* ---------------- 未保存改动的统一守卫 ---------------- */
function openUnsavedDialog(names: string[], withKeep: boolean): Promise<string> {
  return new Promise((resolve) => {
    let settled = false;
    const pick = (v: string): void => {
      if (!settled) {
        settled = true;
        resolve(v);
      }
    };
    const body = document.createElement("div");
    body.className = "pm-confirm-text";
    const p1 = document.createElement("p");
    p1.textContent = "以下分区有尚未保存的修改：" + names.join("、") + "。";
    const p2 = document.createElement("p");
    p2.textContent = "「保存并继续」先提交修改；「放弃修改」恢复原始值。";
    body.appendChild(p1);
    body.appendChild(p2);
    if (withKeep) {
      const p3 = document.createElement("p");
      p3.textContent = "「保留修改继续查看」只切换分区，改动暂不提交。";
      body.appendChild(p3);
    }
    const actions = [{ label: "取消", variant: "ghost", onClick: () => pick("cancel") }];
    if (withKeep) {
      actions.push({ label: "保留修改继续查看", variant: "ghost", onClick: () => pick("keep") });
    }
    actions.push({ label: "放弃修改", variant: "danger", onClick: () => pick("discard") });
    actions.push({ label: "保存并继续", variant: "primary", onClick: () => pick("save") });
    openModal({
      title: "有未保存的修改",
      body,
      actions,
    });
  });
}
// 依次提交每个脏分区；任一取消/失败即中止（已提交的保持已提交，未提交的保留脏状态）。
// 保存写链是单例在途（ops.js 的 busy）：若已有保存在途，组件 save() 会被挡回 false——
// 若不在这里说破，"保存并继续"会静默中止、页签不切换也没有反馈。
async function saveDirty(list: DirtyStore[]): Promise<boolean> {
  const ops = (window as { YB?: { settingsOps?: { isBusy?: () => boolean } } }).YB?.settingsOps;
  for (const s of list) {
    if (ops?.isBusy?.()) {
      toast().info("保存进行中，请稍候再试");
      return false;
    }
    const ok = await s.save();
    if (!ok) return false;
  }
  return true;
}
function guardThen(run: () => void, withKeep: boolean): void {
  const list = dirtyStores();
  if (!list.length) {
    run();
    return;
  }
  void openUnsavedDialog(list.map((s) => s.name), withKeep).then((choice) => {
    if (choice === "cancel") return;
    if (choice === "keep") {
      run();
      return;
    }
    if (choice === "discard") {
      void reloadAll().then(run);
      return;
    }
    void saveDirty(list).then((ok) => {
      if (ok) run();
    });
  });
}

/* ---------------- 站内离场守卫 ---------------- */
function onDocClick(e: MouseEvent): void {
  const t = e.target as Element | null;
  const a = t && t.closest ? (t.closest("a[href]") as HTMLAnchorElement | null) : null;
  if (!a) return;
  const raw = a.getAttribute("href") || "";
  if (!raw || raw.charAt(0) === "#") return;
  if (a.target === "_blank" || a.hasAttribute("download")) return;
  if (/^(mailto:|tel:|javascript:)/i.test(raw)) return;
  if (!dirtyStores().length) return;
  e.preventDefault();
  e.stopPropagation();
  guardThen(() => {
    location.href = a.href;
  }, false);
}

/* ---------------- 子组件回调 ---------------- */
function onQuotaSaved(): void {
  // 容量上限还是调度警示与执行体 KPI 的输入，保存后重拉设置刷新这两处派生状态。
  // **不整体替换 settings.value**：那会让 ScheduleCard/HealthCard/QuotaCard 的 watch 立即
  // apply() 重置表单——用户在别处选了「保留修改继续查看」的未保存改动被静默丢弃（脏标消失
  // 且无提示）。这里只就地更新容量相关字段，两个消费它们的 computed 会随嵌套属性变化重算，
  // 而各卡 watch（deep:false）因对象标识不变不会触发。
  void api<Record<string, unknown>>("GET", "/api/settings")
    .then((data) => {
      if (!data) return;
      if ("capacity" in data) settings.value.capacity = data.capacity;
      if ("capacity_estimate" in data) settings.value.capacity_estimate = data.capacity_estimate;
      execRef.value?.refreshKpis();
    })
    .catch(() => undefined);
}
function onExecutorsData(data: Record<string, unknown> | null): void {
  capAdvice.value = data;
}

let mq: MediaQueryList | null = null;
onMounted(async () => {
  const wanted = new URLSearchParams(location.search).get("tab");
  try {
    const me = (await api<{ is_builtin_admin?: boolean }>("GET", "/api/me")) as { is_builtin_admin?: boolean };
    isMaster.value = !!me?.is_builtin_admin;
  } catch {
    window.location.href = shellBase() + "/login";
    return;
  }
  ready.value = true;
  if (wanted) applyTab(wanted, false);
  await startLoad().catch(() => undefined);
  document.addEventListener("click", onDocClick, true);
});
onUnmounted(() => {
  document.removeEventListener("click", onDocClick, true);
});
</script>

<template>
  <div class="settings-page">
    <div class="page-head">
      <h1 class="page-title">系统设置</h1>
      <p class="page-sub">系统级配置与开关；各分区独立保存。</p>
    </div>

    <p v-if="status" class="alert" :class="status.tone" id="set-status" role="status">
      <span class="ico"><svg aria-hidden="true"><use href="#i-clock" /></svg></span>
      <span class="body" id="set-status-text">{{ status.text }}</span>
      <button v-if="status.retry" type="button" class="btn btn--ghost btn--sm" data-set-retry @click="startLoad().catch(() => undefined)">重试</button>
    </p>

    <!-- 刻意不用 core.js 的 data-tab-group/data-tab-target 契约：那套有 document 级点击委托，
         会先于本页的未保存改动守卫切换分区（legacy 的守卫因此形同虚设）。本页自管页签。 -->
    <div data-settings-tabs>
      <div class="tabs-scroll">
        <div class="tabs" role="tablist" aria-label="设置分区">
          <a
            v-for="t in visibleTabs"
            :key="t.key"
            class="tab"
            :class="{ 'is-active': tab === t.key, 'is-dirty': tabHasDirty(t.key) }"
            :title="tabHasDirty(t.key) ? '有未保存的修改' : undefined"
            role="tab"
            :id="'set-tab-' + t.key"
            :aria-selected="tab === t.key ? 'true' : 'false'"
            :aria-controls="'set-panel-' + t.key"
            href="#"
            :data-settings-tab="t.key"
            :tabindex="tab === t.key ? 0 : -1"
            @click.prevent="selectTab(t.key)"
            @keydown="onTabKeydown($event, t.key)"
          >{{ t.title }}</a>
        </div>
      </div>

      <div class="tab-panel" :class="{ 'is-active': tab === 'schedule' }" role="tabpanel" id="set-panel-schedule" aria-labelledby="set-tab-schedule">
        <ScheduleCard ref="scheduleRef" :settings="settings" :is-master="isMaster" />
      </div>

      <div class="tab-panel" :class="{ 'is-active': tab === 'announcement' }" role="tabpanel" id="set-panel-announcement" aria-labelledby="set-tab-announcement">
        <AnnouncementCard
          ref="annRef"
          :is-master="isMaster"
          :announcement="announcement"
          :on-reload="loadAnnouncement"
        />
      </div>

      <div class="tab-panel" :class="{ 'is-active': tab === 'notify' }" role="tabpanel" id="set-panel-notify" aria-labelledby="set-tab-notify">
        <NotifyCard ref="notifyRef" :is-master="isMaster" />
      </div>

      <div class="tab-panel" :class="{ 'is-active': tab === 'quota' }" role="tabpanel" id="set-panel-quota" aria-labelledby="set-tab-quota">
        <QuotaCard ref="quotaRef" :settings="settings" :is-master="isMaster" :advice="capAdvice" :on-saved="onQuotaSaved" />
      </div>

      <div class="tab-panel" :class="{ 'is-active': tab === 'health' }" role="tabpanel" id="set-panel-health" aria-labelledby="set-tab-health">
        <HealthCard ref="healthRef" :settings="settings" :is-master="isMaster" />
      </div>

      <div v-if="isMaster" class="tab-panel" :class="{ 'is-active': tab === 'executors' }" role="tabpanel" id="set-panel-executors" aria-labelledby="set-tab-executors">
        <ExecutorsCard ref="execRef" :settings="settings" :is-master="isMaster" :on-data="onExecutorsData" />
      </div>

      <div v-if="isMaster" class="tab-panel" :class="{ 'is-active': tab === 'switches' }" role="tabpanel" id="set-panel-switches" aria-labelledby="set-tab-switches">
        <SwitchesCard :settings="settings" :is-master="isMaster" />
      </div>
    </div>
  </div>
</template>
