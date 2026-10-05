<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { api, errorMessage } from "../lib/shell";

/* 执行体与出口分区（整 tab 仅主管理员）。
   legacy = components/settings-executors.js（清单模型：一行一个执行体，写操作走行接口、
   立即落盘，没有"整页保存"）。出口读接口只回脱敏描述串，编辑框一律留空并提示「留空 = 不修改」，
   完整串既不入 DOM 文本与属性，也不进 title / data-*。

   规模 KPI 口径（后端算好，前端只计数与一次除法）：
     «今日进度» = 今日已了结账号数 ÷ 计入容量的账号数；
     «平均每执行体分到的人数» = 计入容量的账号数 ÷ 并行执行体行数（只数「并行」行）；
     «设定的账号容量上限» = 容量配额里的**账号**上限（0 = 不限）。 */

const props = defineProps<{
  settings: Record<string, unknown>;
  isMaster: boolean;
  onData: (data: Record<string, unknown> | null) => void;
}>();

const data = ref<Record<string, unknown> | null>(null);
const banner = ref<{ text: string; kind: string }>({ text: "", kind: "info" });

const TYPE_TEXT: Record<string, string> = { worker: "并行", fallback: "故障转移", disabled: "停用" };
const STATE_TEXT: Record<string, string> = { running: "正在跑本轮", finished: "本轮已跑完", idle: "今天还没跑", stale: "可能被中断" };
const STATE_CLASS: Record<string, string> = { running: "badge--ok", finished: "badge--info", idle: "badge--muted", stale: "badge--bad" };
const FB_TEXT: Record<string, string> = { off: "未启用", running: "运行中", declared_not_running: "已启用·未运行", running_not_declared: "运行中·配置未启用" };
const FB_CLASS: Record<string, string> = { off: "badge--muted", running: "badge--ok", declared_not_running: "badge--bad", running_not_declared: "badge--info" };
const EGRESS_DIRECT = "直连（本机出口）";

function count(v: unknown): number { return Number(v) || 0; }
function attr(v: unknown): string { return v == null ? "" : String(v); }
function executors(): Array<Record<string, unknown>> { return (data.value?.executors as Array<Record<string, unknown>>) || []; }
function activities(): Array<Record<string, unknown>> {
  const a = data.value?.activity as { by_executor?: Array<Record<string, unknown>> } | undefined;
  return (a && a.by_executor) || [];
}
function fb(): Record<string, unknown> { return (data.value?.fallback as Record<string, unknown>) || {}; }
function workers(): Record<string, unknown> { return (data.value?.workers as Record<string, unknown>) || {}; }
function rowName(row: Record<string, unknown>): string {
  if (attr(row.type) !== "fallback" && attr(row.name)) return attr(row.name);
  return attr(row.label) || TYPE_TEXT[attr(row.type)] || "执行体";
}
function rowTitle(row: Record<string, unknown>): string {
  if (attr(row.type) === "disabled") return "执行体（槽位 " + count(row.slot) + "）";
  return rowName(row);
}
function egressText(v: unknown): string {
  const s = attr(v);
  return (!s || s === EGRESS_DIRECT) ? EGRESS_DIRECT : "代理 " + s;
}
function manifestKey(slot: unknown): string {
  const keys = (workers().env_keys as Record<string, string>) || {};
  return attr(keys.manifest) + "[" + count(slot) + "]";
}
function activityFor(role: string, index: number | null): Record<string, unknown> | null {
  for (const a of activities()) {
    if (attr(a.role) === role && (index == null || count(a.index) === index)) return a;
  }
  return null;
}
function dailyText(a: Record<string, unknown> | null): string {
  if (!a) return "—";
  return "成功 " + count(a.done) + " · 失败 " + count(a.failed) + (count(a.claimed) ? " · 进行中 " + count(a.claimed) : "");
}
function fbAbnormal(f: Record<string, unknown>): boolean {
  return f.status === "running_not_declared" || (f.status === "declared_not_running" && f.in_window === true);
}
function fbClass(f: Record<string, unknown>): string {
  if (f.status === "declared_not_running" && f.in_window !== true) return "badge--muted";
  return FB_CLASS[attr(f.status)] || "badge--muted";
}
function fallbackRowMissing(): string {
  const f = fb();
  if (f.status === "off") return "";
  const has = executors().some((r) => attr(r.type) === "fallback");
  return has ? "" : "（清单里已没有故障转移行）";
}
const fbText = computed(() => {
  const f = fb();
  return (f.status === "declared_not_running" ? "已声明开启但没有进程在跑。"
    : (f.status === "running_not_declared" ? "有进程在跑，但不是由配置拉起的。" : "")) + fallbackRowMissing();
});
const fbAlarm = computed(() => fb().status === "declared_not_running" && fb().in_window === true);
const sortedRows = computed(() => {
  return executors().slice().sort((a, b) => {
    const af = attr(a.type) === "fallback" ? 0 : 1, bf = attr(b.type) === "fallback" ? 0 : 1;
    return af !== bf ? af - bf : count(a.slot) - count(b.slot);
  });
});
const unknownActs = computed(() => activities().filter((a) => attr(a.role) === "unknown"));

/* KPI */
function capNum(key: string): number | null {
  const c = (props.settings.capacity as Record<string, unknown>) || null;
  if (!c || c[key] == null) return null;
  return count(c[key]);
}
const kpi = computed(() => {
  const loaded = !!data.value;
  const w = count(workers().configured);
  const acc = capNum("accounts");
  const amax = capNum("accounts_max");
  const totals = ((data.value?.activity as { totals?: Record<string, unknown> }) || {}).totals || {};
  const per = acc != null && w > 0 ? Math.ceil(acc / w) : null;
  return {
    loaded,
    progress: loaded ? count(totals.done) + "/" + count(data.value?.current_accounts) : "—",
    worker: loaded ? String(w) : "—",
    perexec: loaded ? (per == null ? "—" : String(per)) : "—",
    capacity: !loaded ? "—" : (amax === 0 ? "不限" : (amax == null ? "—" : String(amax))),
  };
});
const singleMode = computed(() => !!(data.value && workers().single_mode));

async function load(): Promise<void> {
  if (!props.isMaster) return;
  try {
    const d = (await api<Record<string, unknown>>("GET", "/api/scheduler/executors")) ?? {};
    data.value = d;
    banner.value = { text: "", kind: "info" };
    props.onData(d);
  } catch (e) {
    banner.value = { text: errorMessage(e, "执行体配置读取失败，请稍后重试"), kind: "danger" };
  }
}
function refreshKpis(): void { /* KPI 由 computed 驱动，父组件改 settings 即重算 */ }

function ops(): Record<string, (c: unknown, ...a: unknown[]) => Promise<boolean>> | undefined {
  return (window as { YB?: { settingsOps?: Record<string, (c: unknown, ...a: unknown[]) => Promise<boolean>> } }).YB?.settingsOps;
}
function ctx(): Record<string, unknown> {
  return {
    isMaster: props.isMaster,
    load: () => load(),
    setBusy: () => undefined,
    tip: (t: string, bad: boolean) => { banner.value = { text: t, kind: bad ? "danger" : "success" }; },
    banner: (t: string, kind: string) => { banner.value = { text: t, kind }; },
  };
}
function addRow(): void { void ops()?.executorsAddRow(ctx()); }
function changeType(row: Record<string, unknown>, next: string): void {
  const word = next === "disabled" ? "停用" : "启用";
  void ops()?.executorsChangeType(ctx(), row, next, word);
}
function removeRow(row: Record<string, unknown>): void { void ops()?.executorsRemoveRow(ctx(), row); }

/* ---------- 行内设置弹窗 ---------- */
const dialogOpen = ref(false);
const dlgRow = ref<Record<string, unknown> | null>(null);
const dlgName = ref("");
const dlgEgress = ref("");
const dlgFbEnable = ref(false);
const dlgBusy = ref(false);
/* 窄屏（≤720）：固定 520px 的 el-dialog 在 360/480 会横溢出视口，且 EP 不限高时内容
   实测 1711px、footer（保存）落在视口外——改整屏 sheet（配 app.css 的 .set-exec-dialog）。 */
const narrow = ref(false);
let narrowMq: MediaQueryList | null = null;
function onNarrowChange(e: MediaQueryListEvent | MediaQueryList): void { narrow.value = e.matches; }
onMounted(() => {
  if (typeof window.matchMedia !== "function") return;
  narrowMq = window.matchMedia("(max-width: 720px)");
  onNarrowChange(narrowMq);
  narrowMq.addEventListener("change", onNarrowChange);
});
onUnmounted(() => {
  narrowMq?.removeEventListener("change", onNarrowChange);
  narrowMq = null;
});
function openRow(row: Record<string, unknown>): void {
  dlgRow.value = row;
  dlgName.value = attr(row.name);
  dlgEgress.value = "";
  dlgFbEnable.value = fb().enabled === true;
  dialogOpen.value = true;
}
function clearEgress(): void {
  const row = dlgRow.value;
  if (!row) return;
  dialogOpen.value = false;
  void ops()?.executorsClearEgress(ctx(), row);
}
async function saveRow(): Promise<void> {
  const row = dlgRow.value;
  if (!row || dlgBusy.value) return;
  const slot = count(row.slot);
  const isFb = attr(row.type) === "fallback";
  const egress = String(dlgEgress.value || "").trim();
  const newName = String(dlgName.value || "").trim();
  const nameArg = !isFb && newName !== attr(row.name) ? newName : null;
  const enableArg = isFb && dlgFbEnable.value !== (fb().enabled === true) ? (dlgFbEnable.value ? 1 : 0) : null;
  if (!egress && nameArg == null && enableArg == null) {
    banner.value = { text: "没有需要保存的改动（留空 = 不修改出口；改成直连请点「清除出口」）。", kind: "info" };
    return;
  }
  const rowBody: Record<string, unknown> = {};
  if (egress) rowBody.proxy = egress;
  if (nameArg != null) rowBody.name = nameArg;
  const requests: Array<Record<string, unknown>> = [];
  if (egress || nameArg != null) requests.push({ method: "PUT", path: "/api/scheduler/executors/rows/" + slot, body: rowBody });
  if (enableArg != null) requests.push({ method: "PUT", path: "/api/scheduler/executors", body: { fallback_enable: enableArg } });
  const needsPw = !!egress || enableArg != null;
  const desc = (egress
    ? "修改 " + rowTitle(row) + " 的出口配置" + (enableArg != null ? "与故障转移开关" : "")
    : (enableArg ? "开启" : "关闭") + "故障转移") + "？请输入当前管理员密码确认。";
  dialogOpen.value = false;
  dlgBusy.value = true;
  await ops()?.executorsSaveRow(ctx(), row, { needsPw, requests, desc });
  dlgBusy.value = false;
}
const singleHint = computed(() => {
  const w = workers();
  if (count(w.configured) > 1) return "";
  const key = attr((w.env_keys as Record<string, string>)?.single);
  return "只有一个并行执行体时，程序在本进程内直接签到，出口读全局配置「" + key + "」（不是本行这一格）；加到第二个并行执行体后才按行生效。";
});

defineExpose({ load, refreshKpis });
</script>

<template>
  <section class="kpi-grid" id="set-exec-kpis" aria-label="执行体与容量规模">
    <article class="kpi-card c-primary">
      <div class="kpi-top"><div class="kpi-identity"><div class="kpi-icon primary"><svg aria-hidden="true"><use href="#i-circle-check" /></svg></div><div class="kpi-label">今日进度</div></div></div>
      <div class="kpi-value" id="set-exec-kpi-progress">
        <span v-if="!kpi.loaded" class="skeleton skeleton--title dash-skel" aria-hidden="true" />
        <template v-else>{{ kpi.progress }}</template>
      </div>
    </article>
    <article class="kpi-card c-primary">
      <div class="kpi-top"><div class="kpi-identity"><div class="kpi-icon primary"><svg aria-hidden="true"><use href="#i-server" /></svg></div><div class="kpi-label">并行执行体</div></div></div>
      <div class="kpi-value" id="set-exec-kpi-worker">
        <span v-if="!kpi.loaded" class="skeleton skeleton--title dash-skel" aria-hidden="true" />
        <template v-else>{{ kpi.worker }}</template>
      </div>
    </article>
    <article class="kpi-card c-purple">
      <div class="kpi-top"><div class="kpi-identity"><div class="kpi-icon purple"><svg aria-hidden="true"><use href="#i-users" /></svg></div><div class="kpi-label">平均每执行体分到的人数</div></div></div>
      <div class="kpi-value" id="set-exec-kpi-perexec">
        <span v-if="!kpi.loaded" class="skeleton skeleton--title dash-skel" aria-hidden="true" />
        <template v-else>{{ kpi.perexec }}</template>
      </div>
    </article>
    <article class="kpi-card c-muted">
      <div class="kpi-top"><div class="kpi-identity"><div class="kpi-icon muted"><svg aria-hidden="true"><use href="#i-gauge" /></svg></div><div class="kpi-label">设定的账号容量上限</div></div></div>
      <div class="kpi-value" id="set-exec-kpi-capacity">
        <span v-if="!kpi.loaded" class="skeleton skeleton--title dash-skel" aria-hidden="true" />
        <template v-else>{{ kpi.capacity }}</template>
      </div>
    </article>
  </section>

  <section class="card" id="set-executors-list">
    <div class="panel-head">
      <div class="panel-head-row">
        <h2 class="panel-title">执行体一览</h2>
        <div class="set-head-actions">
          <button type="button" class="btn btn--ghost btn--sm" data-doc="set-pop-executors" data-doc-title="执行体一览与规模卡口径">
            <svg aria-hidden="true"><use href="#i-info" /></svg>口径说明
          </button>
          <div id="set-pop-executors" hidden><b>规模卡：</b>「今日进度」＝今日已了结的账号数 ÷ 计入容量的账号数；「平均每执行体分到的人数」＝计入容量的账号数 ÷ 并行执行体行数（向上取整；只数「并行」行）；「设定的账号容量上限」＝容量配额里的账号上限（不是用户上限，执行体分的是账号），0 = 不限。<b>清单：</b>一行＝一个执行体，上限 64 行（槽位 0~63）；增行用表底的「添加执行体」，改名称/出口点行内「设置」。故障转移行固定置顶、不可改类型与删除。槽位号只增不复用。<b>出口：</b>只显示去处、不含账号密码，行内「设置」里改，留空 = 不修改；改直连用「清除出口」。<b>停用 / 删除：</b>在「操作」列的「更多」里，且要输一次当前管理员密码才生效。<b>写入：</b>改动即时写入配置（本分区没有整页"保存"），下一轮定时任务或重启执行体/容器后生效。</div>
        </div>
      </div>
      <p class="panel-sub" id="set-exec-fb-state" role="status" :hidden="!fbAbnormal(fb())">
        <span class="badge badge--muted" id="set-exec-fb-badge" :class="'badge dot ' + fbClass(fb())">{{ FB_TEXT[attr(fb().status)] || "—" }}</span>
        <span id="set-exec-fb-text">{{ fbText }}</span>
      </p>
    </div>
    <p v-if="banner.text" class="alert" :class="banner.kind" id="set-exec-banner" role="status" aria-live="polite">
      <span class="ico" id="set-exec-banner-ico"><svg aria-hidden="true"><use :href="'#i-' + (banner.kind === 'danger' ? 'circle-x' : banner.kind === 'success' ? 'circle-check' : 'info')" /></svg></span>
      <span class="body" id="set-exec-banner-body">{{ banner.text }}</span>
    </p>
    <div class="table-scroll">
      <table class="data-table" id="set-exec-table">
        <thead>
          <tr>
            <th scope="col">执行体</th><th scope="col">类型</th><th scope="col">状态</th>
            <th scope="col">当日</th><th scope="col">出口</th><th scope="col">操作</th>
          </tr>
        </thead>
        <tbody id="set-exec-assign">
          <tr v-for="r in sortedRows" :key="String(r.slot)" :class="{ 'set-exec-row-off': attr(r.type) === 'disabled' }" :data-slot="String(count(r.slot))">
            <td>
              <span class="mono">{{ rowName(r) }}</span>
              <span v-if="attr(r.type) === 'disabled'" class="set-exec-slot">（槽位 {{ count(r.slot) }}）</span>
            </td>
            <td>{{ TYPE_TEXT[attr(r.type)] || "—" }}</td>
            <td>
              <span class="set-exec-state">
                <span v-if="attr(r.type) === 'disabled'" class="badge dot badge--muted">停用</span>
                <span v-if="attr(r.type) === 'disabled'" class="set-exec-off">不拉起</span>
                <span v-else-if="attr(r.type) === 'fallback'" class="badge dot" :class="fbClass(fb())">{{ FB_TEXT[attr(fb().status)] || "—" }}</span>
                <span v-else class="badge dot" :class="STATE_CLASS[attr(r.state)]">{{ STATE_TEXT[attr(r.state)] || "—" }}</span>
              </span>
            </td>
            <td class="set-exec-daily">{{ attr(r.type) === 'disabled' ? '—' : dailyText(activityFor(attr(r.type) === 'fallback' ? 'fallback' : 'worker', attr(r.type) === 'fallback' ? null : count(r.slot))) }}</td>
            <td>{{ egressText(r.egress) }}</td>
            <td>
              <span class="set-exec-ops">
                <button type="button" class="btn btn--ghost btn--sm set-exec-open" :aria-label="rowName(r) + ' 设置（槽位 ' + count(r.slot) + '）'" @click="openRow(r)">设置</button>
                <el-dropdown v-if="attr(r.type) !== 'fallback'" trigger="click">
                  <button type="button" class="btn btn--ghost btn--icon" :aria-label="rowName(r) + ' 更多操作'" title="更多操作"><svg aria-hidden="true"><use href="#i-ellipsis" /></svg></button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item @click="changeType(r, attr(r.type) === 'disabled' ? 'worker' : 'disabled')">{{ attr(r.type) === 'disabled' ? '启用（改回并行）' : '停用（保留出口与槽位）' }}</el-dropdown-item>
                      <el-dropdown-item class="is-danger" @click="removeRow(r)">删除这一行</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </span>
            </td>
          </tr>
          <tr v-for="a in unknownActs" :key="'unk-' + attr(a.label)">
            <td>{{ attr(a.label) || "未标注（旧数据）" }}</td>
            <td>—</td><td>—</td>
            <td class="set-exec-daily">{{ dailyText(a) }}</td>
            <td>—</td><td />
          </tr>
          <tr v-if="!sortedRows.length && !unknownActs.length">
            <td>清单为空：点表底的「添加执行体」加一行</td>
            <td>—</td><td>—</td><td>—</td><td>—</td><td />
          </tr>
        </tbody>
      </table>
    </div>
    <p class="alert info set-exec-hint" id="set-exec-solo" role="status" :hidden="!singleMode">清单中没有「并行」执行体：定时轮按<b>单执行体形态</b>运行（出口读全局配置）。需要并行请点下面的「添加执行体」。</p>
    <div class="set-exec-addrow">
      <button type="button" class="btn btn--ghost btn--sm" id="set-exec-row-add" :disabled="!isMaster" @click="addRow">添加执行体</button>
    </div>
    <p class="set-warn" id="set-exec-fb-warn" :hidden="!fbAlarm">{{ fbAlarm ? "故障转移行声明已开启但当前没有在跑（且正在签到时段内）：窗口内的漏签不会被补，请检查宿主 cron（或容器调度器）是否以 --fallback 拉起。" : "" }}</p>
  </section>

  <el-dialog v-model="dialogOpen" :title="dlgRow ? (attr(dlgRow.label) || '执行体') : '执行体'" :width="narrow ? '100%' : '520px'" modal-class="set-exec-overlay" class="set-exec-dialog" append-to-body>
    <div class="set-exec-form">
      <p v-if="singleHint" class="alert info set-exec-hint" role="status">{{ singleHint }}</p>
      <div v-if="dlgRow && attr(dlgRow.type) === 'fallback'" class="field">
        <span class="field-label">故障转移开关</span>
        <el-switch v-model="dlgFbEnable" aria-label="开启故障转移" />
        <p class="field-help">只写声明开关：窗口内补签还需部署侧以 --fallback 拉起进程才会真在跑。</p>
      </div>
      <div v-if="dlgRow && attr(dlgRow.type) !== 'fallback'" class="field">
        <label class="field-label" for="set-exec-modal-name">名称（留空 = 用默认名）</label>
        <div class="input-group">
          <input id="set-exec-modal-name" v-model="dlgName" class="input" type="text" autocomplete="off" maxlength="32" :placeholder="attr(dlgRow.label) || '并行执行体'" />
        </div>
        <p class="field-help">只影响本页显示（最长 32 个字符）。</p>
      </div>
      <div class="field">
        <span class="field-label">类型</span>
        <p class="set-summary">{{ TYPE_TEXT[attr(dlgRow?.type)] || "—" }}{{ dlgRow && attr(dlgRow.type) === 'fallback' ? "（固定：置顶、不可改类型、不可删除）" : "" }}</p>
      </div>
      <div class="field">
        <span class="field-label">当前出口（已脱敏）</span>
        <p class="set-summary">{{ egressText(dlgRow?.egress) }}｜配置项 {{ manifestKey(dlgRow?.slot) }}</p>
      </div>
      <div class="field">
        <label class="field-label" for="set-exec-modal-egress">设置出口（留空 = 不修改）</label>
        <div class="input-group">
          <input id="set-exec-modal-egress" v-model="dlgEgress" class="input" type="text" autocomplete="off" spellcheck="false" placeholder="http://user:pass@host:port" />
        </div>
      </div>
      <p class="set-exec-subactions">
        <button type="button" class="linklike" @click="clearEgress">清除出口（改为直连）</button>
      </p>
    </div>
    <template #footer>
      <button type="button" class="btn btn--ghost" @click="dialogOpen = false">取消</button>
      <button type="button" class="btn btn--primary" :disabled="dlgBusy" @click="saveRow">保存</button>
    </template>
  </el-dialog>
</template>
