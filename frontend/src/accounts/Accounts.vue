<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  api,
  dangerousSubmit,
  errorMessage,
  isCanceled,
  ownerEmailVisible,
  passwordHint,
  passwordPolicyOk,
  shellBase,
  swapOut,
  toast,
} from "../lib/shell";
import "./ops.js"; // 注册 window.YB.accountOps（纯 JS，被 Python 侧 node 真跑钉住）
import {
  GROUP_KEYS,
  GROUPS,
  accountMatch,
  badgeOf,
  batchActions,
  countLabel,
  deletedAtText,
  emptyText,
  groupAll,
  lastExecText,
  legendEntries,
  menuItems,
  menuItemsWithout,
  ownerMailText,
  ownerText,
  prefText,
  pruneSelection,
  selectAllState,
  selectedIds,
  selectedPhones,
  stateCell,
  statsOf,
} from "./model.js";
import {
  availableUserItems,
  buildAccountPayload,
  CODE_CLEAR_BTN,
  CODE_CLEAR_CANCEL_BTN,
  CODE_CLEARED_PLACEHOLDER,
  CODE_PLACEHOLDER,
  CODE_UNCHANGED_PLACEHOLDER,
  CREDS_GATE_DESC,
  PASSWORD_UNCHANGED_PLACEHOLDER,
  credsWritten,
  emailBaseItems,
  formSubtitle,
  formTexts,
  formTitle,
  makeSnapshot,
  submitLabel,
  type SelectItem,
} from "../myaccounts/accountform";

/* 账号管理（管理端 /work/accounts）—— P3 整页迁移到 Vue。
   与 legacy 的分工：口径层在 `model.js`（纯函数 + Vitest + node 真跑选中身份键），写操作
   链路在 `ops.js`（纯 JS，被 node 真跑钉住防重入与门禁），本组件只做编排与渲染。
   添加/编辑表单**复用** `src/myaccounts/accountform.ts`（与用户端自提交同一套校验与文案，
   单一口径）；管理端字段与端点（/api/accounts[…]）由本页给出。

   ## 安全硬约束（照抄 legacy）
   1. 列表项 phone / owner 已是服务端脱敏值，直接展示；完整手机号只在编辑详情请求与提交
      请求体内流转，绝不写进常驻 DOM 文本或属性（e2e 断言页面不含完整号）。
   2. 完整号 + 乐观锁快照只存组件内存（fullPhone/snapshot），提交后清空。
   3. 零 `v-html`：全部插值/属性绑定。
   4. 写操作一律经 ops.js（受门禁操作走 shell.dangerousSubmit，档位只存在于后端）。 */

const GROUP_LIST = GROUP_KEYS;

interface AccountRecord {
  index: number;
  name: string;
  phone: string;
  phone_model: string;
  display_name: string;
  owner: string;
  owner_display: string;
  status: string;
  deleted: boolean;
  deleted_at: string;
  user_paused: boolean;
  time_pref: string | null;
  time_pref_edge: string | null;
  last_executor: { role: string; label: string } | null;
  has_phone_code: boolean;
}

interface AccountOps {
  review(a: AccountRecord, action: string): void;
  remove(a: AccountRecord): void;
  restore(a: AccountRecord): void;
  purge(a: AccountRecord): void;
  move(a: AccountRecord, dir: number): void;
  signin(a: AccountRecord): void;
  batch(action: string, ids: number[], phones: string[]): void;
}

const ANIM_MIN_MS = 80;
const TAB_KEYS = GROUP_KEYS;
// 状态图例（静态）：由单源 status-vocab.js 经 model.legendEntries() 派生，覆盖账号表可
// 渲染的全部状态码；模板不再手写 chip。详情见 model.js 该函数与单源总账测试。
const LEGEND = legendEntries();

const accounts = ref<AccountRecord[]>([]);
const states = ref<Record<string, string>>({});
const stateMsgs = ref<Record<string, string>>({});
const stateDurs = ref<Record<string, number | null>>({});
// 「自选」列按后端能力探测隐藏：YIBAN_ALLOW_TIME_PREF 默认 0 时整列恒「—」，
// 留着只会让用户以为功能坏了。口径与用户端横幅同一来源（/api/me 的 time_pref_allowed）。
const me = ref<{ time_pref_allowed?: boolean } | null>(null);
const timePrefOn = computed(() => me.value?.time_pref_allowed === true);
const tab = ref("pending");
const search = ref<Record<string, string>>({ pending: "", active: "", deleted: "" });
const sel = ref<Record<string, Record<string, boolean>>>({ pending: {}, active: {}, deleted: {} });
const busy = ref(false);
const menuOpen = ref(false);
const narrow = ref(false);
const deletedHint = ref(false);
const ownerEmailOn = ref(ownerEmailVisible());
// 写操作就地反馈（响应式，替代 legacy 直接改 DOM 的 markSigning/flashRow）：
// 待签中的账号 index / 刚被移动需高亮的账号 index。由 ops.js 的 ctx 回调驱动。
const signingIndex = ref<number | null>(null);
const flashIndex = ref<number | null>(null);

const rootEl = ref<HTMLElement | null>(null);

/* ---------------- 分组视图 ---------------- */
function rowsOf(group: string): AccountRecord[] {
  const kw = search.value[group] ?? "";
  return groupAll(accounts.value, group).filter((a) => accountMatch(a, kw));
}
function totalOf(group: string): number {
  return groupAll(accounts.value, group).length;
}
function groupCount(group: string): string {
  return countLabel(totalOf(group), rowsOf(group).length, search.value[group] ?? "");
}
function isFiltering(group: string): boolean {
  return totalOf(group) > 0 && rowsOf(group).length === 0;
}
function emptyMessage(group: string): string {
  // 空态文案单一口径在 model.emptyText（Vitest 钉住）：真空给组默认文案，筛出的空给
  // 「无匹配结果」。此处不再本地重写一份。
  return emptyText(group, groupAll(accounts.value, group), rowsOf(group));
}
function selCount(group: string): number {
  return Object.keys(sel.value[group] ?? {}).length;
}
function allState(group: string) {
  return selectAllState(accounts.value, sel.value, group, search.value[group] ?? "");
}
// KPI 是「今日签到统计」总览：口径覆盖**全部**正常账号，不随「正常账号」页的检索框收窄
// （legacy renderStats(active) 用的也是未过滤的 groupAll）。检索只影响表格行。
const kpi = computed(() => statsOf(groupAll(accounts.value, "active"), states.value));
const pendingRows = computed(() => groupAll(accounts.value, "pending"));

// 状态列视图：手动签到触发后本账号先就地显示「待签中」（signingIndex），随后刷新用服务端
// 真实状态覆盖（load() 成功即清空 signingIndex）。legacy 是直接改该行 DOM 的 className。
function stateOf(row: AccountRecord): { tone: string; title: string; icon: string } {
  if (signingIndex.value === row.index) return { tone: "muted", title: "待签中", icon: "clock" };
  return stateCell(row.phone, states.value, stateMsgs.value, stateDurs.value);
}

function toggleRow(group: string, phone: string, on: boolean): void {
  const next = { ...sel.value[group] };
  if (on) next[phone] = true;
  else delete next[phone];
  sel.value = { ...sel.value, [group]: next };
}
function onSelectAll(group: string, on: boolean): void {
  const next: Record<string, boolean> = {};
  if (on) rowsOf(group).forEach((a) => (next[a.phone] = true));
  sel.value = { ...sel.value, [group]: next };
}
function clearSelection(group: string): void {
  sel.value = { ...sel.value, [group]: {} };
  deletedHint.value = false;
}

/* ---------------- 数据加载 ---------------- */
function makeOps(): AccountOps {
  const shell = (window as { YB?: { accountOps?: { create(c: unknown): unknown } } }).YB;
  return (shell?.accountOps?.create({
    busy: (on: boolean) => (busy.value = on),
    refresh: () => load(true),
    onBatchSuccess: () => {
      sel.value = { pending: {}, active: {}, deleted: {} };
    },
    onBatchDelete: () => {
      deletedHint.value = true;
    },
    // 就地反馈改由组件响应式状态驱动（ops.js 不碰 DOM）：手动签到 → 待签中；移动成功 → 高亮。
    onSigning: (index: number) => {
      signingIndex.value = index;
    },
    onFlash: (index: number) => {
      flashIndex.value = index;
      window.setTimeout(() => {
        if (flashIndex.value === index) flashIndex.value = null;
      }, 320);
    },
  }) ?? {}) as unknown as AccountOps;
}

// 写操作实例**单例**：ops.js 的防重入守卫是实例内的 `inflight`（在途时第二次触发早退）。
// 若每次动作都 makeOps() 现建一个，实例内 inflight 每次都是 false，双击 restore/move/批量
// 就会各发一次请求——legacy 是页面初始化时建一次 ops 复用（work_accounts.js）。故这里也
// 懒建一次复用；ctx 回调闭包引用的都是本组件稳定的 ref 与函数，无需重建。
let opsSingleton: AccountOps | null = null;
function ops(): AccountOps {
  if (!opsSingleton) opsSingleton = makeOps();
  return opsSingleton;
}

async function load(animate = false): Promise<void> {
  const t0 = performance.now();
  const data = await api<{
    accounts?: AccountRecord[];
    states?: Record<string, string>;
    state_msgs?: Record<string, string>;
    state_durs?: Record<string, number | null>;
  }>("GET", "/api/accounts");
  const apply = (): void => {
    accounts.value = data.accounts ?? [];
    states.value = data.states ?? {};
    stateMsgs.value = data.state_msgs ?? {};
    stateDurs.value = data.state_durs ?? {};
    sel.value = pruneSelection(accounts.value, sel.value);
    // 服务端真实状态已到，撤掉「待签中」的乐观标记（同 legacy：刷新后由真实状态覆盖）。
    signingIndex.value = null;
  };
  if (animate && performance.now() - t0 > ANIM_MIN_MS && rootEl.value) swapOut(rootEl.value, apply);
  else apply();
}

async function startLoad(): Promise<void> {
  try {
    await load(false);
  } catch (e) {
    toast().error(errorMessage(e, "账号列表加载失败，请稍后重试"));
  }
}

/* ---------------- 动作派发 ---------------- */
function onRowAction(row: AccountRecord, key: string): void {
  const o = ops();
  if (key === "approve") o.review(row, "approve");
  else if (key === "reject") o.review(row, "reject");
  else if (key === "edit") openForm(row.index);
  else if (key === "remove") o.remove(row);
  else if (key === "restore") o.restore(row);
  else if (key === "purge") o.purge(row);
  else if (key === "move_up") o.move(row, -1);
  else if (key === "move_down") o.move(row, 1);
  else if (key === "signin") o.signin(row);
}
function onBatch(group: string, key: string): void {
  const ids = selectedIds(accounts.value, sel.value, group);
  const phones = selectedPhones(accounts.value, sel.value, group);
  const action = key === "remove" ? "delete" : key;
  ops().batch(action, ids, phones);
}

/* ---------------- 页签 ---------------- */
function selectTab(next: string): void {
  if (!TAB_KEYS.includes(next)) return;
  tab.value = next;
  deletedHint.value = false;
}
function syncTabUrl(next: string): void {
  try {
    const params = new URLSearchParams(location.search);
    params.set("tab", next);
    history.replaceState(null, "", `${location.pathname}?${params.toString()}${location.hash}`);
  } catch {
    /* 无 history 环境静默降级 */
  }
}
watch(tab, syncTabUrl);

// WAI-ARIA tabs 键盘导航。本页已脱离 core.js 的 data-tab-group 契约（那套方向键只认
// `.tab[data-tab-target]`，够不到这里的页签），且非活动页签 tabindex=-1 不可 Tab 到达，
// 故组件自管：Left/Right 循环、Home/End 到首/末——移动焦点 + 切换选中 + 同步 roving tabindex。
function onTabKeydown(e: KeyboardEvent, g: string): void {
  const i = TAB_KEYS.indexOf(g);
  if (i < 0) return;
  let next = -1;
  if (e.key === "ArrowRight") next = (i + 1) % TAB_KEYS.length;
  else if (e.key === "ArrowLeft") next = (i - 1 + TAB_KEYS.length) % TAB_KEYS.length;
  else if (e.key === "Home") next = 0;
  else if (e.key === "End") next = TAB_KEYS.length - 1;
  else return;
  e.preventDefault();
  const target = TAB_KEYS[next];
  selectTab(target);
  rootEl.value?.querySelector<HTMLElement>("#acct-tab-" + target)?.focus();
}

/* ---------------- 添加 / 编辑表单 ---------------- */
const formOpen = ref(false);
const formEditing = ref(false);
const formIndex = ref<number | null>(null);
const formError = ref("");
const formBusy = ref(false);
const formDetailLoading = ref(false);
const formSnapshot = ref<string | null>(null);
const fullPhone = ref("");
const f = ref({ name: "", phone: "", password: "", model: "", code: "" });
const clearCode = ref(false);
// 固定条目（不绑定 / 手填邮箱（未注册）/ 分组头）**先同步就位**，再异步补可绑定用户——
// legacy 是 mount() 里先 setOptions(emailBaseItems()) 再 loadAvailableUsers。接口失败时
// 保留了固定条目，手填路径仍可达（否则下拉连「手填邮箱」都没有）。
const emailItems = ref<SelectItem[]>(emailBaseItems());
const formEmail = ref("");
const formManualEmail = ref("");
const formInitialPassword = ref("");
const T = computed(() => formTexts("admin"));
const showManual = computed(() => !formEditing.value && formEmail.value === "__manual__");
const editingAccount = computed(() => (formIndex.value != null ? accounts.value[formIndex.value] : null));
function onMenuVisible(v: boolean): void {
  menuOpen.value = v;
}

function openForm(index: number | null): void {
  formEditing.value = index !== null;
  formIndex.value = index;
  formError.value = "";
  formSnapshot.value = null;
  fullPhone.value = "";
  clearCode.value = false;
  formEmail.value = "";
  formManualEmail.value = "";
  formInitialPassword.value = "";
  // 先落固定条目（同步），loadBindableUsers 成功后再覆盖为「固定条目 + 可绑定用户」。
  emailItems.value = emailBaseItems();
  const a = index !== null ? accounts.value[index] : null;
  f.value = {
    name: a?.name ?? "",
    phone: a?.phone ?? "",
    password: "",
    model: a?.phone_model ?? "",
    code: "",
  };
  formOpen.value = true;
  if (index !== null) void loadDetail(index);
  else void loadBindableUsers();
}
function openAdd(): void {
  openForm(null);
}

async function loadDetail(index: number): Promise<void> {
  formDetailLoading.value = true;
  try {
    const d = await api<{ account?: { phone?: string; name?: string; phone_model?: string; status?: string; deleted?: boolean } }>(
      "GET",
      `/api/accounts/${index}/detail`,
    );
    const acc = d.account ?? {};
    // 迟到响应防串号：弹窗已关，或已改成编辑另一个账号（index 变），都不得再写入表单。
    if (!formOpen.value || formIndex.value !== index) return;
    if (!accounts.value[index]?.phone) {
      formSnapshot.value = null;
      fullPhone.value = "";
      f.value.phone = "";
      return;
    }
    fullPhone.value = String(acc.phone ?? "");
    formSnapshot.value = makeSnapshot(acc);
    f.value.phone = accounts.value[index].phone; // 只回显打码号
  } catch {
    // 同判据：过期/串号的失败响应不得清掉当前（可能是另一个）表单的完整号与快照。
    if (!formOpen.value || formIndex.value !== index) return;
    formSnapshot.value = null;
    fullPhone.value = "";
  } finally {
    formDetailLoading.value = false;
  }
}

async function loadBindableUsers(): Promise<void> {
  try {
    const data = await api<{ users?: Array<{ email?: string; display?: string; account_count?: number }> }>("GET", "/api/users");
    emailItems.value = availableUserItems(data.users ?? []);
  } catch {
    /* 静默：保持已同步就位的固定条目（不绑定 / 手填邮箱）——手填路径不因接口失败而消失 */
  }
}

function toggleClearCode(): void {
  clearCode.value = !clearCode.value;
  if (clearCode.value) f.value.code = "";
}

// 弹窗关闭（取消 / ESC / 遮罩 / 保存成功）后清掉仅应在弹窗生命周期内驻留的敏感态：
// 完整手机号与乐观锁快照。兑现组件文档「绝不写进常驻 DOM、提交后清空」的承诺。
function onFormClosed(): void {
  fullPhone.value = "";
  formSnapshot.value = null;
}

async function submitForm(): Promise<void> {
  if (formBusy.value) return;
  formError.value = "";
  const built = buildAccountPayload(
    {
      name: f.value.name,
      phoneVisible: f.value.phone,
      phoneFull: fullPhone.value,
      password: f.value.password,
      phoneModel: f.value.model,
      phoneCode: f.value.code,
      clearCode: clearCode.value,
      email: !formEditing.value ? formEmail.value : undefined,
      manualEmail: formManualEmail.value,
      initialPassword: formInitialPassword.value,
    },
    {
      editing: formEditing.value,
      allowEmail: true,
      requiresSnapshot: formEditing.value,
      snapshot: formSnapshot.value,
      policyOk: passwordPolicyOk,
      policyHint: passwordHint(false),
    },
  );
  if (built.error || !built.payload) {
    formError.value = built.error ?? "表单校验未通过";
    return;
  }
  const payload = built.payload;
  const endpoint = formEditing.value ? `/api/accounts/${formIndex.value}` : "/api/accounts";
  const method = formEditing.value ? "PUT" : "POST";
  const gated = credsWritten(payload, { editing: formEditing.value, snapshot: formSnapshot.value });
  formBusy.value = true;
  try {
    const data = gated
      ? ((await dangerousSubmit({ method, path: endpoint, body: payload, desc: CREDS_GATE_DESC })) as { msg?: string })
      : ((await api(method, endpoint, payload)) as { msg?: string });
    formOpen.value = false;
    fullPhone.value = "";
    formSnapshot.value = null;
    toast().success(data?.msg || "已保存");
    await load(true);
  } catch (e) {
    if (isCanceled(e)) return; // 取消门禁口令框不算失败
    formError.value = errorMessage(e, "保存失败，请稍后再试");
  } finally {
    formBusy.value = false;
  }
}

/* ---------------- 轮询 / 断点 ---------------- */
let timer: ReturnType<typeof setInterval> | null = null;
let mq: MediaQueryList | null = null;
function pollTick(): void {
  if (document.visibilityState !== "visible") return;
  if (busy.value || menuOpen.value) return;
  if (document.querySelector(".pm-backdrop, .el-overlay")) return;
  void load(true).catch(() => undefined);
}
function onVisibility(): void {
  pollTick();
}
function onBreak(e: MediaQueryListEvent): void {
  narrow.value = e.matches;
}

onMounted(async () => {
  const wanted = new URLSearchParams(location.search).get("tab");
  if (wanted && TAB_KEYS.includes(wanted)) tab.value = wanted;
  if (window.matchMedia) {
    mq = window.matchMedia("(max-width: 900px)");
    narrow.value = mq.matches;
    mq.addEventListener("change", onBreak);
  }
  try {
    me.value = await api<{ time_pref_allowed?: boolean }>("GET", "/api/me");
  } catch {
    window.location.href = shellBase() + "/login";
    return;
  }
  await startLoad();
  timer = setInterval(pollTick, 10000);
  document.addEventListener("visibilitychange", onVisibility);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
  document.removeEventListener("visibilitychange", onVisibility);
  if (mq) mq.removeEventListener("change", onBreak);
});

// 供模板使用（避免在模板里直接引用 import 的纯函数名导致可读性下降）
const M = {
  badgeOf,
  batchActions,
  deletedAtText,
  lastExecText,
  menuItems,
  menuItemsWithout,
  ownerMailText,
  ownerText,
  prefText,
  stateCell,
};
</script>

<template>
  <div class="accounts-page">
    <div class="page-head page-head--split">
      <div class="page-head-text">
        <h1 class="page-title">账号管理</h1>
        <p class="page-sub">管理易班账号：审核、排序与手动签到；删除后 7 天内可恢复。</p>
      </div>
      <button type="button" class="btn btn--primary" data-add-account @click="openAdd">
        <svg aria-hidden="true"><use href="#i-plus" /></svg>添加账号
      </button>
    </div>

    <div id="accounts-root" ref="rootEl">
      <div data-acct-group>
        <div class="tabs-scroll">
          <div class="tabs" role="tablist" aria-label="账号管理分区">
            <a
              v-for="g in GROUP_LIST"
              :key="g"
              class="tab"
              :class="{ 'is-active': tab === g }"
              role="tab"
              :id="'acct-tab-' + g"
              :aria-selected="tab === g ? 'true' : 'false'"
              :aria-controls="'acct-panel-' + g"
              href="#"
              :data-acct-tab="g"
              :tabindex="tab === g ? 0 : -1"
              @click.prevent="selectTab(g)"
              @keydown="onTabKeydown($event, g)"
            >
              {{ GROUPS[g].title }} <span class="acct-count" :id="GROUPS[g].count">{{ groupCount(g) }}</span>
            </a>
          </div>
        </div>

        <div
          v-for="g in GROUP_LIST"
          :key="g"
          class="tab-panel"
          :class="{ 'is-active': tab === g }"
          role="tabpanel"
          :id="'acct-panel-' + g"
          :aria-labelledby="'acct-tab-' + g"
          :data-acct-panel="g"
          :data-tab-id="g"
        >
          <!-- 待处理页：今日签到统计 KPI + 待处理提醒（同一页） -->
          <section v-if="g === 'pending'" class="kpi-grid" aria-label="今日签到统计">
            <article class="kpi-card c-success">
              <div class="kpi-top"><div class="kpi-identity">
                <div class="kpi-icon success"><svg aria-hidden="true"><use href="#i-circle-check" /></svg></div>
                <div class="kpi-label">今日成功</div>
              </div></div>
              <div class="kpi-value" id="stat-success">{{ kpi.success }}</div>
            </article>
            <article class="kpi-card c-danger">
              <div class="kpi-top"><div class="kpi-identity">
                <div class="kpi-icon danger"><svg aria-hidden="true"><use href="#i-circle-x" /></svg></div>
                <div class="kpi-label">今日失败</div>
              </div></div>
              <div class="kpi-value" id="stat-failed">{{ kpi.failed }}</div>
            </article>
            <article class="kpi-card c-info">
              <div class="kpi-top"><div class="kpi-identity">
                <div class="kpi-icon info"><svg aria-hidden="true"><use href="#i-clock" /></svg></div>
                <div class="kpi-label">待签</div>
              </div></div>
              <div class="kpi-value" id="stat-waiting">{{ kpi.waiting }}</div>
            </article>
            <article class="kpi-card c-muted">
              <div class="kpi-top"><div class="kpi-identity">
                <div class="kpi-icon muted"><svg aria-hidden="true"><use href="#i-circle-slash" /></svg></div>
                <div class="kpi-label">跳过</div>
              </div></div>
              <div class="kpi-value" id="stat-skipped">{{ kpi.skipped }}</div>
            </article>
          </section>

          <!-- 口径说明：账号页 KPI 与数据看板「今日成功/失败/跳过」同名但分母不同
               （此处=账号数、当日终态每账号计一次；看板=事件/尝试数，重试各算一次）。 -->
          <p v-if="g === 'pending'" class="panel-sub acct-kpi-note" id="acct-kpi-note">
            口径：按<strong>账号</strong>统计（当日最终状态，每账号计一次）；与数据看板的「事件」口径不同。
          </p>

          <p
            v-if="g === 'pending'"
            class="alert warning acct-pending-tip"
            id="pending-tip"
            role="status"
            :hidden="pendingRows.length === 0"
          >
            <span class="ico"><svg aria-hidden="true"><use href="#i-clock" /></svg></span>
            <span class="body">有 <strong id="pending-count">{{ pendingRows.length }}</strong> 个账号待处理，请及时审核。</span>
          </p>

          <section class="card">
            <div class="panel-head acct-group-head">
              <div class="panel-head-row">
                <h2 class="panel-title">{{ GROUPS[g].title }}</h2>
                <div class="acct-tools">
                  <label class="acct-search">
                    <span class="sr-only">{{ GROUPS[g].searchLabel }}</span>
                    <input v-model="search[g]" type="search" class="input" :id="g + '-search'" placeholder="名称 / 手机号 / 用户名" />
                  </label>
                </div>
              </div>
              <p class="panel-sub">{{ GROUPS[g].sub }}</p>
            </div>

            <div :id="g + '-body'" class="collapse-body is-open">
              <div class="collapse-inner">
                <!-- 状态图例：由单源 `lib/status-vocab.js` 经 `model.legendEntries()` 派生，
                     覆盖账号表**可渲染的全部**状态码（新增 no_position/global_paused 后曾只列 8 项）。
                     同 symbol 同色（icon + 账号页语气档）的状态合并为一格、标签列出全部 full 名；
                     本处不再手写 chip 表，改词表即改图例。 -->
                <p v-if="g === 'active'" class="acct-legend">
                  <span
                    v-for="it in LEGEND"
                    :key="it.icon + '|' + it.tone"
                    class="acct-state"
                    :class="'acct-state--' + it.tone"
                  >
                    <svg aria-hidden="true"><use :href="'#i-' + it.icon" /></svg>{{ it.text }}
                  </span>
                </p>

                <div class="acct-batch" :id="GROUPS[g].bar" role="status" aria-live="polite" :hidden="selCount(g) === 0">
                  <span class="acct-batch-count">已选 <strong :id="GROUPS[g].cnt">{{ selCount(g) }}</strong> 个</span>
                  <div class="acct-batch-actions">
                    <button
                      v-for="a in batchActions(g)"
                      :key="a.key"
                      type="button"
                      class="btn btn--sm"
                      :class="a.variant"
                      :data-batch="g + ':' + a.key"
                      @click="onBatch(g, a.key)"
                    >
                      {{ a.label }}
                    </button>
                    <button type="button" class="btn btn--ghost btn--sm" :data-batch-clear="g" @click="clearSelection(g)">
                      取消选择
                    </button>
                  </div>
                </div>

                <p v-if="g === 'active' && deletedHint" class="alert info acct-deleted-hint" data-deleted-hint="active" role="status">
                  <span class="body">已移入『待删除账号』，7 天内可恢复。</span>
                  <button type="button" class="btn btn--ghost btn--sm" @click="selectTab('deleted')">去待删除账号</button>
                </p>

                <div class="table-scroll">
                  <table class="data-table acct-table" :aria-label="GROUPS[g].title">
                    <!-- 待处理表头 -->
                    <thead v-if="g === 'pending'">
                      <tr>
                        <th scope="col" class="acct-cell-check"><label class="acct-check"><input type="checkbox" :id="GROUPS.pending.all" aria-label="全选待处理账号" :checked="allState('pending').checked" :indeterminate="allState('pending').indeterminate" @change="onSelectAll('pending', ($event.target as HTMLInputElement).checked)" /></label></th>
                        <th scope="col">审核</th>
                        <th scope="col" class="acct-cell-name">名称</th>
                        <th scope="col" class="acct-cell-phone">手机号</th>
                        <th scope="col" class="acct-col-lg acct-cell-lastexec" title="最近一次有记录的业务日实际领取该账号的执行体">上次实领</th>
                        <th scope="col" class="acct-col-md acct-cell-owner">归属</th>
                        <th scope="col" class="acct-cell-actions">操作</th>
                      </tr>
                    </thead>
                    <!-- 正常账号表头 -->
                    <thead v-else-if="g === 'active'">
                      <tr>
                        <th scope="col" class="acct-cell-check"><label class="acct-check"><input type="checkbox" :id="GROUPS.active.all" aria-label="全选正常账号" :checked="allState('active').checked" :indeterminate="allState('active').indeterminate" @change="onSelectAll('active', ($event.target as HTMLInputElement).checked)" /></label></th>
                        <th scope="col">状态</th><th scope="col" title="账号在账号列表中的位置（含待处理与待删除项），非行号">序号</th>
                        <th scope="col" class="acct-cell-name">名称</th>
                        <th scope="col" class="acct-cell-phone">手机号</th>
                        <th scope="col" class="acct-col-lg acct-cell-lastexec" title="最近一次有记录的业务日实际领取该账号的执行体">上次实领</th>
                        <th scope="col" class="acct-col-lg">设备型号</th><th v-if="timePrefOn" scope="col" class="acct-col-xl">自选</th>
                        <th scope="col" class="acct-col-md acct-cell-owner">归属</th>
                        <th scope="col" class="acct-cell-actions">操作</th>
                      </tr>
                    </thead>
                    <!-- 待删除表头 -->
                    <thead v-else>
                      <tr>
                        <th scope="col" class="acct-cell-check"><label class="acct-check"><input type="checkbox" :id="GROUPS.deleted.all" aria-label="全选待删除账号" :checked="allState('deleted').checked" :indeterminate="allState('deleted').indeterminate" @change="onSelectAll('deleted', ($event.target as HTMLInputElement).checked)" /></label></th>
                        <th scope="col" class="acct-cell-name">名称</th>
                        <th scope="col" class="acct-cell-phone">手机号</th>
                        <th scope="col" class="acct-col-lg acct-cell-lastexec" title="最近一次有记录的业务日实际领取该账号的执行体">上次实领</th>
                        <th scope="col" class="acct-col-md acct-cell-owner">归属</th>
                        <th scope="col">删除时间</th>
                        <th scope="col" class="acct-cell-actions">操作</th>
                      </tr>
                    </thead>

                    <tbody :id="GROUPS[g].tbody">
                      <!-- key 用 index（服务端唯一位置键）而非脱敏 phone：脱敏号前 3 后 4 相同
                          的两个账号会撞成同一个 key（138****8000），导致 Vue diff 错配。 -->
                      <tr
                        v-for="row in rowsOf(g)"
                        :key="g + ':' + row.index"
                        :data-acct-idx="row.index"
                        :class="{ 'acct-row-flash': flashIndex === row.index }"
                      >
                        <td class="acct-cell-check">
                          <label class="acct-check">
                            <input type="checkbox" :aria-label="'选择账号 ' + row.display_name" :checked="!!sel[g][row.phone]" @change="toggleRow(g, row.phone, ($event.target as HTMLInputElement).checked)" />
                          </label>
                        </td>

                        <!-- 待处理行 -->
                        <template v-if="g === 'pending'">
                          <td class="acct-cell-audit"><span class="badge" :class="'badge--' + M.badgeOf(row.status).tone">{{ M.badgeOf(row.status).label }}</span></td>
                          <td class="acct-cell-name">
                            <div class="acct-name-main">{{ row.display_name }}</div>
                            <span v-if="ownerEmailOn" class="acct-owner-inline">{{ M.ownerMailText(row) }}</span>
                          </td>
                          <td class="acct-cell-phone">{{ row.phone }}</td>
                          <td class="acct-cell-lastexec acct-col-lg">{{ M.lastExecText(row) }}</td>
                          <td class="acct-cell-owner acct-col-md">{{ M.ownerText(row) }}</td>
                        </template>

                        <!-- 待删除行 -->
                        <template v-else-if="g === 'deleted'">
                          <td class="acct-cell-name">
                            <div class="acct-name-main">{{ row.display_name }}<span class="badge badge--bad">待删除</span></div>
                            <span v-if="ownerEmailOn" class="acct-owner-inline">{{ M.ownerMailText(row) }}</span>
                          </td>
                          <td class="acct-cell-phone">{{ row.phone }}</td>
                          <td class="acct-cell-lastexec acct-col-lg">{{ M.lastExecText(row) }}</td>
                          <td class="acct-cell-owner acct-col-md">{{ M.ownerText(row) }}</td>
                          <td class="acct-cell-time">{{ M.deletedAtText(row) }}</td>
                        </template>

                        <!-- 正常账号行 -->
                        <template v-else>
                          <td class="acct-cell-state">
                            <span class="acct-state" :class="'acct-state--' + stateOf(row).tone" :title="stateOf(row).title" :aria-label="stateOf(row).title">
                              <svg aria-hidden="true"><use :href="'#i-' + stateOf(row).icon" /></svg>
                            </span>
                          </td>
                          <td class="acct-cell-idx">{{ row.index + 1 }}</td>
                          <td class="acct-cell-name">
                            <div class="acct-name-main">{{ row.display_name }}</div>
                            <span v-if="ownerEmailOn" class="acct-owner-inline">{{ M.ownerMailText(row) }}</span>
                          </td>
                          <td class="acct-cell-phone">{{ row.phone }}</td>
                          <td class="acct-cell-lastexec acct-col-lg">{{ M.lastExecText(row) }}</td>
                          <td class="acct-cell-model acct-col-lg">{{ row.phone_model || "—" }}</td>
                          <td v-if="timePrefOn" class="acct-cell-pref acct-col-xl">{{ M.prefText(row) }}</td>
                          <td class="acct-cell-owner acct-col-md">{{ M.ownerText(row) }}</td>
                        </template>

                        <!-- 操作列：宽屏并列按钮 / 窄屏收进下拉（与 legacy 断点一致） -->
                        <td class="acct-cell-actions">
                          <div v-if="!narrow" class="acct-row-actions">
                            <template v-if="g === 'pending'">
                              <button type="button" class="btn btn--primary btn--sm" @click="onRowAction(row, 'approve')">通过</button>
                              <button type="button" class="btn btn--ghost btn--sm" @click="onRowAction(row, 'reject')">驳回</button>
                              <button type="button" class="btn btn--ghost btn--icon" aria-label="编辑" title="编辑" @click="onRowAction(row, 'edit')"><svg aria-hidden="true"><use href="#i-pencil" /></svg></button>
                              <button type="button" class="btn btn--ghost btn--icon btn--danger-ghost" aria-label="删除" title="删除" @click="onRowAction(row, 'remove')"><svg aria-hidden="true"><use href="#i-trash" /></svg></button>
                            </template>
                            <template v-else-if="g === 'deleted'">
                              <button type="button" class="btn btn--ghost btn--sm" @click="onRowAction(row, 'restore')">恢复</button>
                              <button type="button" class="btn btn--ghost btn--sm btn--danger-ghost" @click="onRowAction(row, 'purge')">彻底删除</button>
                            </template>
                            <template v-else>
                              <button type="button" class="btn btn--ghost btn--icon" aria-label="上移" title="上移" @click="onRowAction(row, 'move_up')"><svg aria-hidden="true"><use href="#i-arrow-up" /></svg></button>
                              <button type="button" class="btn btn--ghost btn--icon" aria-label="下移" title="下移" @click="onRowAction(row, 'move_down')"><svg aria-hidden="true"><use href="#i-arrow-down" /></svg></button>
                              <button type="button" class="btn btn--ghost btn--sm" @click="onRowAction(row, 'signin')">签到</button>
                              <button type="button" class="btn btn--ghost btn--icon" aria-label="编辑" title="编辑" @click="onRowAction(row, 'edit')"><svg aria-hidden="true"><use href="#i-pencil" /></svg></button>
                              <button type="button" class="btn btn--ghost btn--icon btn--danger-ghost" aria-label="删除" title="删除" @click="onRowAction(row, 'remove')"><svg aria-hidden="true"><use href="#i-trash" /></svg></button>
                            </template>
                          </div>
                          <div v-else class="acct-row-actions acct-row-actions--narrow">
                            <button v-if="g === 'pending'" type="button" class="btn btn--primary btn--sm" @click="onRowAction(row, 'approve')">通过</button>
                            <el-dropdown trigger="click" @visible-change="onMenuVisible">
                              <button type="button" class="btn btn--ghost btn--icon" aria-label="更多操作" title="更多操作"><svg aria-hidden="true"><use href="#i-ellipsis" /></svg></button>
                              <template #dropdown>
                                <el-dropdown-menu>
                                  <el-dropdown-item
                                    v-for="it in g === 'pending' ? M.menuItemsWithout('pending', ['approve']) : M.menuItems(g)"
                                    :key="it.key"
                                    :class="{ 'is-danger': it.danger }"
                                    @click="onRowAction(row, it.key)"
                                  >
                                    {{ it.label }}
                                  </el-dropdown-item>
                                </el-dropdown-menu>
                              </template>
                            </el-dropdown>
                          </div>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <p class="empty" :id="GROUPS[g].empty" :hidden="rowsOf(g).length > 0">
                  <span class="empty__icon"><svg aria-hidden="true"><use href="#i-list" /></svg></span>
                  <span class="empty__msg">{{ emptyMessage(g) }}</span>
                  <span class="empty__action">
                    <button
                      v-if="isFiltering(g)"
                      type="button"
                      class="btn btn--ghost btn--sm"
                      :data-empty-clear="g"
                      @click="search[g] = ''"
                    >清除筛选</button>
                    <button
                      v-else-if="GROUPS[g].emptyAction.tab"
                      type="button"
                      class="btn btn--ghost btn--sm"
                      :data-empty-tab="GROUPS[g].emptyAction.tab"
                      @click="selectTab(GROUPS[g].emptyAction.tab as string)"
                    >{{ GROUPS[g].emptyAction.label }}</button>
                    <button
                      v-else
                      type="button"
                      class="btn btn--ghost btn--sm"
                      data-add-account
                      @click="openAdd"
                    >{{ GROUPS[g].emptyAction.label }}</button>
                  </span>
                </p>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>

    <!-- 添加 / 编辑账号弹窗（校验复用 myaccounts/accountform.ts） -->
    <el-dialog
      v-model="formOpen"
      :title="formTitle({ editing: formEditing, variant: 'admin', index: formIndex })"
      width="560px"
      append-to-body
      @closed="onFormClosed"
    >
      <p class="panel-sub" style="margin-top: 0">{{ formSubtitle({ editing: formEditing, variant: 'admin' }) }}</p>
      <div v-if="formError" class="alert danger" role="alert" style="margin-bottom: 10px">
        <span class="ico"><svg aria-hidden="true"><use href="#i-circle-alert" /></svg></span>
        <span class="body">{{ formError }}</span>
      </div>

      <div class="form-stack">
        <label class="field">
          <span class="field-label">{{ T.nameLabel }}</span>
          <input v-model="f.name" class="input" type="text" maxlength="50" :placeholder="T.namePlaceholder" />
          <span class="field-help">{{ T.nameHelp }}</span>
        </label>

        <label v-if="!formEditing" class="field">
          <span class="field-label">绑定用户（选填）</span>
          <!-- filterable：可绑定用户最多 50 个（后端上限），不给筛选就只能滚。
               legacy 自研下拉的筛选框因无人设置 data-search 而成死代码，EP 需显式开。 -->
          <el-select v-model="formEmail" filterable style="width: 100%">
            <!-- 分组头/空态是纯展示行（legacy select-field 里不可选），禁用并给唯一占位值，
                 避免与「不绑定」的空值选项撞成同一个可选项。 -->
            <el-option
              v-for="(it, idx) in emailItems"
              :key="idx"
              :value="it.v ?? `__meta__${idx}`"
              :label="it.t ?? it.group ?? it.empty ?? ''"
              :disabled="it.v === undefined"
            />
          </el-select>
        </label>

        <div v-if="showManual" class="form-stack">
          <label class="field">
            <span class="field-label">手填邮箱</span>
            <input v-model="formManualEmail" class="input" type="email" maxlength="64" placeholder="输入未注册邮箱（自动注册并进入待审核）" />
          </label>
          <label class="field">
            <span class="field-label">初始密码</span>
            <input v-model="formInitialPassword" class="input" type="password" autocomplete="new-password" maxlength="64" :placeholder="'初始密码（' + passwordHint(false) + '）'" />
          </label>
        </div>

        <label class="field">
          <span class="field-label">{{ T.phoneLabel }}<span class="req" aria-hidden="true">*</span></span>
          <input v-model="f.phone" class="input" type="tel" inputmode="numeric" maxlength="20" :placeholder="T.phonePlaceholder" />
          <span v-if="formEditing" class="field-help">{{ T.phoneEditHelp }}</span>
          <span v-if="formDetailLoading" class="field-help">正在获取账号完整信息…</span>
        </label>

        <label class="field">
          <span class="field-label">{{ T.passwordLabel }}<span v-if="!formEditing" class="req" aria-hidden="true">*</span></span>
          <input v-model="f.password" class="input" type="password" autocomplete="new-password" :placeholder="formEditing ? PASSWORD_UNCHANGED_PLACEHOLDER : T.passwordNewPlaceholder" />
        </label>

        <label class="field">
          <span class="field-label">{{ T.modelLabel }}</span>
          <input v-model="f.model" class="input" type="text" maxlength="50" placeholder="按易班 App 设备绑定页填写" />
          <span class="field-help">{{ T.modelHelp }}</span>
        </label>

        <label class="field">
          <span class="field-label">{{ T.codeLabel }}</span>
          <input
            v-model="f.code"
            class="input"
            type="text"
            maxlength="100"
            :readonly="clearCode"
            :placeholder="clearCode ? CODE_CLEARED_PLACEHOLDER : (formEditing && editingAccount?.has_phone_code) ? CODE_UNCHANGED_PLACEHOLDER : CODE_PLACEHOLDER"
          />
          <span class="field-help">{{ T.codeHelp }}</span>
          <button
            v-if="formEditing && (editingAccount?.has_phone_code || clearCode)"
            type="button"
            class="btn btn--ghost btn--sm"
            @click="toggleClearCode"
          >{{ clearCode ? CODE_CLEAR_CANCEL_BTN : CODE_CLEAR_BTN }}</button>
        </label>
      </div>
      <template #footer>
        <button type="button" class="btn btn--ghost" @click="formOpen = false">取消</button>
        <button type="button" class="btn btn--primary" :disabled="formBusy || formDetailLoading" @click="submitForm">
          {{ submitLabel({ editing: formEditing, variant: 'admin' }) }}
        </button>
      </template>
    </el-dialog>
  </div>
</template>
