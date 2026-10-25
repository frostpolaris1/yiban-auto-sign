<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  api,
  confirmDialog,
  dangerousSubmit,
  errorMessage,
  isCanceled,
  ownerEmailVisible,
  passwordHint,
  passwordPolicyOk,
  pollMs,
  shellBase,
  toast,
} from "../lib/shell";
import { usePolling } from "../lib/polling";
import { useTabIndicator } from "../lib/tab-indicator";
import "./ops.js"; // 注册 window.YB.accountOps（纯 JS，被 Python 侧 node 真跑钉住）
import {
  GROUP_KEYS,
  GROUPS,
  badgeOf,
  batchActions,
  countLabel,
  deletedAtText,
  emptyText,
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
} from "./model.js";
import { DEFAULT_PAGE_SIZE, PAGE_SIZES } from "../lib/pager";
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
import {
  PRIVACY_FIELDS,
  buildPrivacyPayload,
  emptyPrivacyState,
  exposureHint,
  privacyConfirmBody,
  privacyDiff,
  privacyEndpoint,
  privacyFromView,
  privacyTitle,
  profileLine,
  type PrivacyState,
} from "../myaccounts/privacy";

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

type Stats = { success: number; failed: number; waiting: number; skipped: number };
interface ListPayload {
  accounts?: AccountRecord[];
  states?: Record<string, string>;
  state_msgs?: Record<string, string>;
  state_durs?: Record<string, number | null>;
  group_totals?: Record<string, number>;
  stats?: Stats;
  total?: number;
  notModified?: boolean;
}

// 服务端分组分页：每组只保存当前页的行，total/组总量/KPI 由服务端给。
const rowsByGroup = ref<Record<string, AccountRecord[]>>({ pending: [], active: [], deleted: [] });
const totals = ref<Record<string, number>>({ pending: 0, active: 0, deleted: 0 });
const groupTotals = ref<Record<string, number>>({ pending: 0, active: 0, deleted: 0 });
const stats = ref<Stats>({ success: 0, failed: 0, waiting: 0, skipped: 0 });
const tab = ref("pending");
const accounts = computed<AccountRecord[]>(() => rowsByGroup.value[tab.value] ?? []);
const allRows = computed<AccountRecord[]>(() =>
  GROUP_LIST.flatMap((g) => rowsByGroup.value[g] ?? []));
function rowByIndex(index: number): AccountRecord | undefined {
  return allRows.value.find((a) => a.index === index);
}
// 状态表按组下发（只含该组当前页的手机号）：分组保存，切页签取当前组那份。
const statesByGroup = ref<Record<string, Record<string, string>>>({ pending: {}, active: {}, deleted: {} });
const stateMsgsByGroup = ref<Record<string, Record<string, string>>>({ pending: {}, active: {}, deleted: {} });
const stateDursByGroup = ref<Record<string, Record<string, number | null>>>({ pending: {}, active: {}, deleted: {} });
const states = computed<Record<string, string>>(() => statesByGroup.value[tab.value] ?? {});
const stateMsgs = computed<Record<string, string>>(() => stateMsgsByGroup.value[tab.value] ?? {});
const stateDurs = computed<Record<string, number | null>>(() => stateDursByGroup.value[tab.value] ?? {});
const me = ref<{ time_pref_allowed?: boolean } | null>(null);
const timePrefOn = computed(() => me.value?.time_pref_allowed === true);
const tabsEl = ref<HTMLElement | null>(null);
const { style: tabIndicatorStyle, ready: tabIndicatorReady } = useTabIndicator(tabsEl, tab);
const search = ref<Record<string, string>>({ pending: "", active: "", deleted: "" });
// 分页状态（每组各持一页；页大小三组共用一个值）。服务端按组过滤后切片，
// 组总量（group_totals）与 KPI（stats）不随当前页收窄。
const pages = ref<Record<string, number>>({ pending: 1, active: 1, deleted: 1 });
const pageSize = ref(DEFAULT_PAGE_SIZE);
const PAGE_SIZE_OPTS: number[] = [...PAGE_SIZES];
const sel = ref<Record<string, Record<string, boolean>>>({ pending: {}, active: {}, deleted: {} });
const busy = ref(false);
const narrow = ref(false);
// 窄屏行操作抽屉：点「更多操作」从右侧滑出；动作集仍从 model.menuItems 取，
// 与宽屏按钮同一份数据源（pending 组省略已外置的「通过」）。
const drawerOpen = ref(false);
const drawerGroup = ref("active");
const drawerRow = ref<AccountRecord | null>(null);
const drawerTrigger = ref<HTMLElement | null>(null);
// 动作会打开确认/编辑/口令弹窗时，抽屉离场不归还焦点，交给新弹窗。
const drawerRestoreFocus = ref(true);
const deletedHint = ref(false);
const ownerEmailOn = ref(ownerEmailVisible());
const signingIndex = ref<number | null>(null);
const flashIndex = ref<number | null>(null);

const rootEl = ref<HTMLElement | null>(null);

/* ---------------- 分组视图（行集来自服务端，已是该组当前页） ---------------- */
function rowsOf(group: string): AccountRecord[] {
  return rowsByGroup.value[group] ?? [];
}
function totalOf(group: string): number {
  return totals.value[group] ?? 0;
}
function groupCount(group: string): string {
  const kw = search.value[group] ?? "";
  if (!kw) return countLabel(groupTotals.value[group] ?? 0, 0, "");
  return String(totalOf(group)) + " 个匹配 / 共 " + String(groupTotals.value[group] ?? 0) + " 个";
}
function pageRows(group: string): AccountRecord[] {
  return rowsOf(group);
}
function pageTotal(group: string): number {
  return totalOf(group);
}
async function onPageChange(group: string, p: number): Promise<void> {
  pages.value = { ...pages.value, [group]: p };
  sel.value = { ...sel.value, [group]: {} };
  try {
    await fetchGroup(group);
  } catch (e) {
    toast().error(errorMessage(e, "账号列表加载失败，请稍后重试"));
  }
}
async function onPageSizeChange(size: number): Promise<void> {
  pageSize.value = size;
  pages.value = { pending: 1, active: 1, deleted: 1 };
  sel.value = { pending: {}, active: {}, deleted: {} };
  try {
    await load(false);
  } catch (e) {
    toast().error(errorMessage(e, "账号列表加载失败，请稍后重试"));
  }
}

const composing = ref(false);

function commitSearch(group: string): void {
  if (composing.value) return;
  rootEl.value?.querySelector<HTMLInputElement>("#" + group + "-search")?.blur();
}
function isFiltering(group: string): boolean {
  return !!search.value[group] && totalOf(group) === 0 && (groupTotals.value[group] ?? 0) > 0;
}
function emptyMessage(group: string): string {
  if (rowsOf(group).length) return "";
  if (isFiltering(group)) return "无匹配结果";
  return emptyText(group, [], []);
}
function selCount(group: string): number {
  return Object.keys(sel.value[group] ?? {}).length;
}
function allState(group: string) {
  return selectAllState(rowsOf(group), sel.value, group, "");
}
const kpi = computed(() => stats.value);
const pendingRows = computed(() => groupTotals.value.pending ?? 0);

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

let opsSingleton: AccountOps | null = null;
function ops(): AccountOps {
  if (!opsSingleton) opsSingleton = makeOps();
  return opsSingleton;
}

function groupQuery(group: string): string {
  const params = new URLSearchParams({
    group,
    limit: String(pageSize.value),
    offset: String(((pages.value[group] ?? 1) - 1) * pageSize.value),
  });
  const kw = search.value[group] ?? "";
  if (kw) params.set("q", kw);
  return "/api/accounts?" + params.toString();
}

// 每组记住「当前内存态对应的查询串」：换筛选/翻页后第一次请求跳过条件请求，
// 否则会拿别处的 ETag 换回 304，页面停在旧结果上。
const lastQuery: Record<string, string> = {};

// 每组一个请求序号：慢响应后到时不得覆盖更新的页码或检索结果（最新请求胜出）。
const reqSeq: Record<string, number> = {};

async function fetchGroup(group: string): Promise<boolean> {
  const seq = (reqSeq[group] ?? 0) + 1;
  reqSeq[group] = seq;
  const q = groupQuery(group);
  const conditional = lastQuery[group] === q;
  const data = await api<ListPayload>("GET", q, null, conditional ? { conditional: true } : undefined);
  if (reqSeq[group] !== seq) return false;
  if (data.notModified) return false;
  lastQuery[group] = q;
  const rows = data.accounts ?? [];
  rowsByGroup.value = { ...rowsByGroup.value, [group]: rows };
  const total = typeof data.total === "number" ? data.total : rows.length;
  totals.value = { ...totals.value, [group]: total };
  if (data.group_totals) groupTotals.value = { ...groupTotals.value, ...data.group_totals };
  if (data.stats) stats.value = data.stats;
  statesByGroup.value = { ...statesByGroup.value, [group]: data.states ?? {} };
  stateMsgsByGroup.value = { ...stateMsgsByGroup.value, [group]: data.state_msgs ?? {} };
  stateDursByGroup.value = { ...stateDursByGroup.value, [group]: data.state_durs ?? {} };
  const last = Math.max(1, Math.ceil(total / pageSize.value));
  if ((pages.value[group] ?? 1) > last) {
    pages.value = { ...pages.value, [group]: last };
    return fetchGroup(group);
  }
  return true;
}

async function load(_animate = false): Promise<void> {
  const changed = await Promise.all(GROUP_LIST.map((g) => fetchGroup(g)));
  if (!changed.some(Boolean)) return;
  sel.value = pruneSelection(allRows.value, sel.value);
  signingIndex.value = null;
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
  const ids = selectedIds(rowsOf(group), sel.value, group);
  const phones = selectedPhones(rowsOf(group), sel.value, group);
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
// 检索条件变化即回到第 1 页（否则筛完停在旧页码上、可能落在越界空页）。
// 服务端检索：250ms 防抖后重拉三组；选中集清空（旧行可能已不在结果里）。
let searchTimer = 0;
watch(search, () => {
  pages.value = { pending: 1, active: 1, deleted: 1 };
  sel.value = { pending: {}, active: {}, deleted: {} };
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => { void load(false); }, 250);
}, { deep: true });

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
/* 二段：隐私资料与暴露（qmz4）。管理面写口 fail-closed（必须带 phone 令牌），
   详情面只下发开着的字段；保存只提交改动过的键（见 myaccounts/privacy.ts）。 */
const detailProfile = ref<Record<string, string>>({});
const privacy = ref<PrivacyState>(emptyPrivacyState());
const privacyInitial = ref<PrivacyState>(emptyPrivacyState());
const privacyError = ref("");
const privacyBusy = ref(false);
const T = computed(() => formTexts("admin"));
const showManual = computed(() => !formEditing.value && formEmail.value === "__manual__");
const editingAccount = computed(() => (formIndex.value != null ? rowByIndex(formIndex.value) : null));
function openActions(row: AccountRecord, group: string, ev?: Event): void {
  drawerTrigger.value = ev?.currentTarget instanceof HTMLElement ? ev.currentTarget : null;
  drawerGroup.value = group;
  drawerRow.value = row;
  drawerRestoreFocus.value = true;
  drawerOpen.value = true;
}
function hasVisibleOverlay(): boolean {
  // 弹层关闭后节点仍留在 body（display:none），不能按存在性判断。
  return Array.from(document.querySelectorAll<HTMLElement>(".pm-backdrop, .el-overlay")).some((node) => {
    const cs = getComputedStyle(node);
    return cs.display !== "none" && cs.visibility !== "hidden" && node.getBoundingClientRect().width > 0;
  });
}
function onDrawerClosed(): void {
  // 彻底删除会先发请求、后开口令框，弹窗可能晚于抽屉离场；按动作预判，不能只看当前可见层。
  if (!drawerRestoreFocus.value) {
    drawerRestoreFocus.value = true;
    return;
  }
  // 动作若已打开确认/编辑弹窗，焦点归它；此处抢回会破坏新弹窗的焦点陷阱。
  if (hasVisibleOverlay()) return;
  // 无新弹窗时把焦点还给「更多操作」；行被移走则退回当前页签，避免焦点落回 body。
  const trigger = drawerTrigger.value;
  if (trigger && trigger.isConnected) trigger.focus();
  else document.querySelector<HTMLElement>('[data-acct-tab="' + tab.value + '"]')?.focus();
}
const drawerActions = computed(() => {
  const g = drawerGroup.value;
  return g === "pending" ? menuItemsWithout("pending", ["approve"]) : menuItems(g);
});
function onDrawerAction(key: string): void {
  const row = drawerRow.value;
  // approve/reject/edit/remove 同步开弹窗；purge 的密码框在请求返回后才开。
  drawerRestoreFocus.value = !["approve", "reject", "edit", "remove", "purge"].includes(key);
  drawerOpen.value = false;
  if (row) onRowAction(row, key);
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
  detailProfile.value = {};
  privacy.value = emptyPrivacyState();
  privacyInitial.value = emptyPrivacyState();
  privacyError.value = "";
  // 先落固定条目（同步），loadBindableUsers 成功后再覆盖为「固定条目 + 可绑定用户」。
  emailItems.value = emailBaseItems();
  const a = index !== null ? rowByIndex(index) : null;
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
    const d = await api<{
      account?: {
        phone?: string;
        name?: string;
        phone_model?: string;
        status?: string;
        deleted?: boolean;
        profile?: Record<string, string>;
      };
    }>("GET", `/api/accounts/${index}/detail`);
    const acc = d.account ?? {};
    // 迟到响应防串号：弹窗已关，或已改成编辑另一个账号（index 变），都不得再写入表单。
    if (!formOpen.value || formIndex.value !== index) return;
    const row = rowByIndex(index);
    if (!row?.phone) {
      formSnapshot.value = null;
      fullPhone.value = "";
      f.value.phone = "";
      return;
    }
    fullPhone.value = String(acc.phone ?? "");
    formSnapshot.value = makeSnapshot(acc);
    f.value.phone = row.phone; // 只回显打码号
    // 详情面只含开着的字段（#201 暴露出口）：键存在＝开关开，内容即展示值。
    detailProfile.value = acc.profile ?? {};
    privacy.value = privacyFromView(acc.profile);
    privacyInitial.value = privacyFromView(acc.profile);
    privacyError.value = "";
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
  detailProfile.value = {};
  privacy.value = emptyPrivacyState();
  privacyInitial.value = emptyPrivacyState();
  privacyError.value = "";
}

/**
 * 二段保存（管理面）：必须带完整号令牌（后端 fail-closed）。
 * 详情只下发开着的字段，故保存后重新取详情，让展示与开关回到服务端事实。
 */
async function submitPrivacyAdmin(): Promise<void> {
  if (privacyBusy.value || !formEditing.value || formIndex.value == null) return;
  privacyError.value = "";
  const payload = buildPrivacyPayload(privacy.value, privacyInitial.value, fullPhone.value, {
    requirePhone: true,
  });
  if (payload && "error" in payload) {
    privacyError.value = payload.error;
    return;
  }
  if (!payload) {
    toast().success("没有需要保存的改动");
    return;
  }
  const diff = privacyDiff(privacy.value, privacyInitial.value);
  const ok = await confirmDialog({
    title: privacyTitle(),
    body: privacyConfirmBody(diff),
    confirmText: "保存",
  });
  if (!ok) return;
  const index = formIndex.value;
  privacyBusy.value = true;
  try {
    await api("PUT", privacyEndpoint("admin", index), payload);
    toast().success("隐私资料已保存");
    await loadDetail(index);
  } catch (e) {
    if (isCanceled(e)) return;
    privacyError.value = errorMessage(e, "隐私资料保存失败，请稍后再试");
  } finally {
    privacyBusy.value = false;
  }
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

/* ---------------- 轮询 / 断点 ----------------
   机制（节拍 / 可见性 / 卸载清理）在 `lib/polling.ts`；本页只给**策略**：
   弹窗或行菜单打开时不刷新（重渲染会打断在途操作与选择态）。节拍取外壳唯一来源
   `pollMs()`（core.js 的 POLL_MS）。 */
let mq: MediaQueryList | null = null;
const pollingOn = ref(false);
function pollGate(): boolean {
  if (busy.value || drawerOpen.value) return false;
  return !hasVisibleOverlay();
}
function onBreak(e: MediaQueryListEvent): void {
  narrow.value = e.matches;
}
usePolling(() => void load(true).catch(() => undefined), {
  intervalMs: pollMs(),
  gate: pollGate,
  active: () => pollingOn.value,
});

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
  pollingOn.value = true;   // 首屏取数完成后才开始轮询
});

onUnmounted(() => {
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
        <p class="page-sub">审核、排序与手动签到；删除后 7 天内可恢复。</p>
      </div>
      <button type="button" class="btn btn--primary" data-add-account @click="openAdd">
        <svg aria-hidden="true"><use href="#i-plus" /></svg>添加账号
      </button>
    </div>

    <div id="accounts-root" ref="rootEl">
      <div data-acct-group>
        <div class="tabs-scroll">
          <div ref="tabsEl" class="tabs tabs--indicator" role="tablist" aria-label="账号管理分区">
            <span class="tabs-indicator" :class="{ 'is-ready': tabIndicatorReady }" :style="tabIndicatorStyle" aria-hidden="true"></span>
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
            :hidden="pendingRows === 0"
          >
            <span class="ico"><svg aria-hidden="true"><use href="#i-clock" /></svg></span>
            <span class="body">有 <strong id="pending-count">{{ pendingRows }}</strong> 个账号待处理，请及时审核。</span>
          </p>

          <section class="card">
            <div class="panel-head acct-group-head">
              <div class="panel-head-row">
                <h2 class="panel-title">账号列表</h2>
                <div class="acct-tools">
                  <!-- 检索是一段**表单**：移动端键盘的「搜索/前往」键只有落到表单提交才有去处
                       （原来只有裸 input + v-model，那个键按下去什么也不发生）。
                       组合输入（中文输入法）期的回车不检索，由 form 上的 composing 标志挡住
                       那次隐式提交（浏览器不为 isComposing 跳过隐式提交）。 -->
                  <form
                    class="acct-search"
                    role="search"
                    :aria-label="GROUPS[g].searchLabel"
                    @submit.prevent="commitSearch(g)"
                    @compositionstart="composing = true"
                    @compositionend="composing = false"
                  >
                    <label class="sr-only" :for="g + '-search'">{{ GROUPS[g].searchLabel }}</label>
                    <input v-model="search[g]" type="search" class="input" :id="g + '-search'" placeholder="名称 / 手机号 / 用户名" />
                    <!-- 可读名沿用同一份口径（可见「搜索」逐字保留，WCAG 2.5.3） -->
                    <button type="submit" class="btn btn--ghost btn--sm" :aria-label="GROUPS[g].searchLabel">搜索</button>
                  </form>
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
                <details v-if="g === 'active'" class="acct-legend">
                  <summary>状态图例（{{ LEGEND.length }} 项）</summary>
                  <div class="acct-legend-grid">
                    <span
                      v-for="it in LEGEND"
                      :key="it.icon + '|' + it.tone"
                      class="acct-state"
                      :class="'acct-state--' + it.tone"
                    >
                      <svg aria-hidden="true"><use :href="'#i-' + it.icon" /></svg>{{ it.text }}
                    </span>
                  </div>
                </details>

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
                        <th scope="col" class="acct-cell-audit">审核</th>
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
                        <th scope="col">状态</th><th scope="col" class="acct-cell-idx" title="账号在账号列表中的位置（含待处理与待删除项），非行号">序号</th>
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
                        <th scope="col" class="acct-col-md">删除时间</th>
                        <th scope="col" class="acct-cell-actions">操作</th>
                      </tr>
                    </thead>

                    <tbody :id="GROUPS[g].tbody">
                      <!-- key 用 index（服务端唯一位置键）而非脱敏 phone：脱敏号前 3 后 4 相同
                          的两个账号会撞成同一个 key（138****8000），导致 Vue diff 错配。 -->
                      <tr
                        v-for="row in pageRows(g)"
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
                          <td class="acct-cell-time acct-col-md">{{ M.deletedAtText(row) }}</td>
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
                            <button type="button" class="btn btn--ghost btn--icon" aria-label="更多操作" title="更多操作" :data-acct-actions="row.index" @click="openActions(row, g, $event)">
                              <svg aria-hidden="true"><use href="#i-ellipsis" /></svg>
                            </button>
                          </div>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <!-- 客户端分页控件（每页大小 × 组）：只渲染当前页，5k 账号下首屏 DOM 与
                     页大小同阶。控件走 EP（不是原生 select/range：原生弹出层样式不可控）。 -->
                <div v-if="pageTotal(g) > pageSize" class="acct-pager" :data-acct-pager="g">
                  <el-pagination
                    :current-page="pages[g]"
                    :page-size="pageSize"
                    :total="pageTotal(g)"
                    :page-sizes="PAGE_SIZE_OPTS"
                    layout="sizes, prev, pager, next"
                    background
                    @current-change="(p: number) => onPageChange(g, p)"
                    @size-change="(s: number) => onPageSizeChange(s)"
                  />
                </div>

                <!-- 空态走 EP `el-empty`（uz7w：手搓空态换自带件）。外层 .empty 手搓
                     构件不再承担布局：图/说明/动作三段由 el-empty 的 image/description/
                     default 三个槽给出。e2e 与 Python 契约按 `#<g>-empty` 与
                     `.empty__msg` / `data-empty-*` 取值，故这些锚点逐字保留。 -->
                <el-empty
                  :id="GROUPS[g].empty"
                  :hidden="rowsOf(g).length > 0"
                  :image-size="48"
                >
                  <template #description>
                    <span class="empty__msg">{{ emptyMessage(g) }}</span>
                  </template>
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
                </el-empty>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>

    <!-- 窄屏行操作抽屉：右侧滑入，保留账号摘要与完整动作列表 -->
    <el-drawer
      v-model="drawerOpen"
      direction="rtl"
      size="min(320px, 88vw)"
      :with-header="false"
      :trap-focus="true"
      title="账号操作"
      aria-label="账号操作"
      class="acct-actions-drawer"
      append-to-body
      @closed="onDrawerClosed"
    >
      <div class="acct-drawer">
        <div class="acct-drawer-head">
          <div class="acct-drawer-account">
            <div class="acct-drawer-name">{{ drawerRow?.display_name }}</div>
            <div class="acct-drawer-phone">{{ drawerRow?.phone }}</div>
          </div>
          <button type="button" class="btn btn--ghost btn--icon" aria-label="关闭操作菜单" @click="drawerOpen = false">
            <svg aria-hidden="true"><use href="#i-x" /></svg>
          </button>
        </div>
        <div class="acct-drawer-actions">
          <button
            v-for="it in drawerActions"
            :key="it.key"
            type="button"
            class="btn btn--ghost acct-drawer-action"
            :class="{ 'btn--danger-ghost': it.danger }"
            :data-acct-action="it.key"
            @click="onDrawerAction(it.key)"
          >
            <svg aria-hidden="true"><use :href="'#i-' + it.icon" /></svg>{{ it.label }}
          </button>
        </div>
      </div>
    </el-drawer>

    <!-- 添加 / 编辑账号弹窗（校验复用 myaccounts/accountform.ts） -->
    <el-dialog
      v-model="formOpen"
      :title="formTitle({ editing: formEditing, variant: 'admin', index: formIndex })"
      width="min(560px, calc(100vw - 32px))"
      modal-class="app-form-overlay"
      class="app-form-dialog"
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
          <span class="field-help">{{ T.passwordHelp }}</span>
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

        <!-- 二段：隐私资料与暴露（qmz4）。只在编辑态可见：详情面只含开着的字段。 -->
        <details v-if="formEditing" class="privacy-details" data-admin-privacy>
          <summary>{{ privacyTitle() }}</summary>
          <p class="field-help">
            详情只显示开着的字段；管理列表始终不显示。保存只提交改动过的键，关闭字段的既有内容不受影响。
          </p>
          <div v-if="profileLine(detailProfile)" class="account-profile" data-detail-profile>
            {{ profileLine(detailProfile) }}
          </div>
          <el-alert v-if="privacyError" type="error" :title="privacyError" :closable="false" show-icon style="margin-bottom: 10px" />
          <div v-for="pf in PRIVACY_FIELDS" :key="pf.key" class="field privacy-field" :data-privacy-field="pf.key">
            <span class="field-label">{{ pf.label }}</span>
            <el-input v-model="privacy.profile[pf.key]" :maxlength="pf.maxlength" clearable :placeholder="pf.placeholder" />
            <div class="privacy-expose">
              <el-switch v-model="privacy.expose[pf.key]" :aria-label="`暴露${pf.label}`" />
              <span class="field-help">{{ exposureHint(privacy.expose[pf.key], 'admin') }}</span>
            </div>
          </div>
          <button type="button" class="btn btn--ghost btn--sm" :disabled="privacyBusy || formDetailLoading" @click="submitPrivacyAdmin">
            保存隐私资料
          </button>
        </details>
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