<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import {
  api,
  confirmDialog,
  errorMessage,
  promptDialog,
  refreshNavBadges,
  shellBase,
  toast,
} from "../lib/shell";
import { DEFAULT_PAGE_SIZE } from "../lib/pager";
import ObjectDrawer from "./ObjectDrawer.vue";
import {
  type AccountDetail,
  type DrawerState,
  type GroupKey,
  type OpRequest,
  type QueueListResponse,
  type QueueRow,
  type QueueSummaryData,
  type RowView,
  closeDrawer,
  deleteRequest,
  openDrawer,
  pendingQuery,
  queueQuery,
  retryRequest,
  reviewRequest,
  rowView,
  summaryView,
  type SummaryView,
} from "./model";

/**
 * 工作队列页（lcpx 试点 2026-10-25，管理端 /work/queue）：既有 8 页之外的**额外入口**。
 *
 * 三组待办（每组一个服务端过滤视图，行数 ≤ 页大小）：
 *   · 待审核 = 既有 `GET /api/accounts?group=pending`（绝对 index，直接复用审核端点）；
 *   · 今日失败 / 今日未签 = 新只读聚合 `GET /api/queue?bucket=…`；
 *   · 今日摘要（业务日/窗口/计数）随 `/api/queue` 响应下发。
 *
 * 行内操作走**既有 ops 端点**：审核（POST /api/accounts/<idx>/review）、软删
 * （DELETE /api/accounts/<idx>）、重试（详情取完整号 → POST /api/signin，号码只在
 * 请求体）。确认族一律经确认桥（epConfirm，Promise<boolean>），驳回理由经
 * promptDialog；写操作防重入（单 inflight 位）。动作成功后刷新受影响的组并叫
 * 外壳重取导航徽标（审核会改变待处理数）。
 */

const PAGE_SIZE = DEFAULT_PAGE_SIZE;

interface GroupState {
  rows: RowView[];
  total: number;
  page: number;
  loading: boolean;
}

const GROUP_SPECS: Array<{ key: GroupKey; title: string; emptyText: string }> = [
  { key: "pending", title: "待审核", emptyText: "已全部审核 · 今日签到见『今日未签』" },
  { key: "failed", title: "今日失败", emptyText: "今日暂无失败" },
  { key: "unsigned", title: "今日未签", emptyText: "没有待签账号" },
];

const summary = ref<QueueSummaryData | null>(null);
const summaryCards = computed<SummaryView | null>(() =>
  summary.value ? summaryView(summary.value) : null);
const errorText = ref("");
const groups = reactive<Record<GroupKey, GroupState>>({
  pending: { rows: [], total: 0, page: 1, loading: false },
  failed: { rows: [], total: 0, page: 1, loading: false },
  unsigned: { rows: [], total: 0, page: 1, loading: false },
});
const drawer = reactive<DrawerState>({ open: false, target: null });
const inflight = ref(false);

async function loadGroup(g: GroupKey): Promise<void> {
  const st = groups[g];
  st.loading = true;
  try {
    const offset = (st.page - 1) * PAGE_SIZE;
    if (g === "pending") {
      const resp = await api<{ accounts?: QueueRow[]; total?: number }>(
        "GET", pendingQuery(PAGE_SIZE, offset),
      );
      st.rows = (resp.accounts ?? []).map((r) => rowView(r, "pending"));
      st.total = Number(resp.total) || st.rows.length;
    } else {
      const resp = await api<QueueListResponse>("GET", queueQuery(g, PAGE_SIZE, offset));
      st.rows = (resp.rows ?? []).map((r) => rowView(r, g));
      st.total = Number(resp.total) || st.rows.length;
      summary.value = {
        today: resp.today, weekday: resp.weekday, window: resp.window, counts: resp.counts,
      };
    }
  } catch (err) {
    errorText.value = errorMessage(err, "网络错误，请重试");
  } finally {
    st.loading = false;
  }
}

function loadAll(): void {
  errorText.value = "";
  void loadGroup("pending");
  void loadGroup("failed");
  void loadGroup("unsigned");
}

function goPage(g: GroupKey, page: number): void {
  const st = groups[g];
  if (st.loading || page === st.page) return;
  st.page = page;
  void loadGroup(g);
}

/* ---------------- 写操作（既有 ops 端点 + 确认桥） ---------------- */

/** 统一链路：防重入 → 请求 → 成功/失败提示。返回是否成功（调用方据此刷新）。 */
async function runWrite(make: () => Promise<{ msg?: string }>, fallback: string): Promise<boolean> {
  if (inflight.value) return false;
  inflight.value = true;
  try {
    const data = await make();
    toast().success(data?.msg || fallback);
    return true;
  } catch (err) {
    toast().error(errorMessage(err, "操作失败，请稍后再试"));
    return false;
  } finally {
    inflight.value = false;
  }
}

async function send(req: OpRequest): Promise<{ msg?: string }> {
  return api<{ msg?: string }>(req.method, req.path, req.body);
}

/** 审核（通过/驳回）。成功后账号可能换组：三组与摘要全刷，徽标同步重取。 */
async function doReview(row: RowView, action: "approve" | "reject", reason?: string): Promise<void> {
  const ok = await confirmDialog({
    title: action === "approve" ? "通过账号" : "驳回账号",
    body: action === "approve"
      ? `确定通过「${row.name}」(${row.phone}) 吗？通过后将参与定时签到。`
      : `确定驳回「${row.name}」(${row.phone}) 吗？理由：${reason}`,
    confirmText: action === "approve" ? "通过" : "驳回",
  });
  if (!ok) return;
  const done = await runWrite(
    () => send(reviewRequest(row, action, reason)),
    action === "approve" ? "已通过" : "已拒绝",
  );
  if (done) {
    refreshNavBadges();
    drawer.target = null;
    loadAll();
  }
}

async function doReject(row: RowView): Promise<void> {
  const raw = await promptDialog({
    title: "驳回账号",
    label: "驳回理由（用户会看到，最多 100 字）",
    placeholder: "请填写驳回理由",
    maxlength: 100,
    required: true,
    requiredMessage: "驳回理由不能为空",
    confirmText: "驳回",
    cancelText: "取消",
  });
  if (raw == null) return; // 取消：不动作
  const reason = raw.trim();
  if (!reason) {
    toast().error("驳回理由不能为空");
    return;
  }
  await doReview(row, "reject", reason);
}

async function doRemove(row: RowView): Promise<void> {
  const req = deleteRequest(row);
  const ok = await confirmDialog({
    title: "删除账号",
    body: `确定删除「${row.name}」（${row.phone}）吗？\n移入『待删除账号』，7 天内可恢复。`,
    confirmText: "删除",
    danger: true,
  });
  if (!ok) return;
  const done = await runWrite(() => send(req), "已删除账号 · 可在『待删除账号』恢复（7 天内）");
  if (done) {
    refreshNavBadges();
    drawer.target = null;
    loadAll();
  }
}

/** 重试（手动签到）：完整号经详情接口取，只进请求体（retryRequest 有形态守卫）。 */
async function doRetry(row: RowView): Promise<void> {
  if (inflight.value) return;
  inflight.value = true;
  try {
    const detail = await api<AccountDetail>("GET", `/api/accounts/${row.index}/detail`);
    const req = retryRequest(row, detail);
    if (!req) {
      toast().error("账号信息不完整，请刷新后重试");
      return;
    }
    const data = await send(req);
    toast().success(data?.msg || "已触发手动签到");
    void loadGroup("failed");
    void loadGroup("unsigned");
  } catch (err) {
    toast().error(errorMessage(err, "操作失败，请稍后再试"));
  } finally {
    inflight.value = false;
  }
}

function openDetail(row: RowView): void {
  Object.assign(drawer, openDrawer(drawer, row));
}

function onDrawerAction(payload: { key: "approve" | "reject" | "edit" | "remove"; row: RowView }): void {
  if (payload.key === "approve") void doReview(payload.row, "approve");
  else if (payload.key === "reject") void doReject(payload.row);
  else if (payload.key === "remove") void doRemove(payload.row);
  else if (payload.key === "edit") {
    // 试点口径：编辑表单留在账号管理页完成（显式跳转，不做半份编辑表单）
    window.location.href = `${shellBase()}/work/accounts`;
  }
}

function closeDrawer_(): void {
  Object.assign(drawer, closeDrawer(drawer));
}

function stateMsgFor(row: RowView): string {
  if (row.group === "pending" && row.rejectReason) return row.rejectReason;
  return row.stateMsg;
}

onMounted(loadAll);
</script>

<template>
  <div class="queue">
    <el-alert v-if="errorText" type="error" :title="errorText" :closable="false" show-icon />

    <!-- 顶端一行今日摘要：业务日 / 窗口 / 账号数 / 失败数（数据随 /api/queue 下发） -->
    <section class="queue-summary" data-queue-summary>
      <template v-if="summaryCards">
        <span class="queue-summary-date">{{ summaryCards.dateText }}</span>
        <span class="queue-summary-item">{{ summaryCards.windowText }}</span>
        <span class="queue-summary-item">账号 {{ summaryCards.accountCount }}</span>
        <span class="queue-summary-item" :class="{ 'is-bad': summaryCards.failedCount > 0 }">
          失败 {{ summaryCards.failedCount }}
        </span>
        <span class="queue-summary-item">待审核 {{ summaryCards.pendingCount }}</span>
      </template>
      <span v-else class="queue-summary-loading">摘要加载中…</span>
    </section>

    <section
      v-for="g in GROUP_SPECS"
      :key="g.key"
      class="queue-group"
      :data-queue-group="g.key"
    >
      <header class="queue-group-head">
        <span class="queue-group-label">{{ g.title }}</span>
        <span class="queue-group-count" data-queue-count>{{ groups[g.key].total }}</span>
        <span v-if="groups[g.key].loading" class="queue-group-loading">加载中…</span>
        <el-pagination
          v-if="groups[g.key].total > PAGE_SIZE"
          class="queue-group-pages"
          :current-page="groups[g.key].page"
          :page-size="PAGE_SIZE"
          :total="groups[g.key].total"
          layout="prev, pager, next"
          small
          background
          @current-change="(p: number) => goPage(g.key, p)"
        />
      </header>

      <el-skeleton v-if="groups[g.key].loading && !groups[g.key].rows.length" :rows="2" animated />
      <p v-else-if="!groups[g.key].rows.length" class="queue-empty">{{ g.emptyText }}</p>
      <ul v-else class="queue-rows">
        <li
          v-for="row in groups[g.key].rows"
          :key="`${row.group}:${row.index}`"
          class="queue-row"
          data-queue-row
          :data-phone="row.phone"
          @click="openDetail(row)"
        >
          <span class="queue-row-name">{{ row.name }}</span>
          <span class="queue-row-phone mono">{{ row.phone }}</span>
          <span class="queue-row-model">{{ row.model || "—" }}</span>
          <span
            class="queue-row-state"
            :class="{
              'is-bad': row.group !== 'pending' && row.group !== 'unsigned',
              'is-warn': row.group === 'pending',
            }"
          >{{ row.badgeText }}</span>
          <span class="queue-row-msg">{{ stateMsgFor(row) || (row.planAt ? `计划 ${row.planAt}` : "—") }}</span>
          <span class="queue-row-actions" @click.stop>
            <template v-if="g.key === 'pending'">
              <el-button
                type="primary"
                size="small"
                data-queue-action="approve"
                @click="doReview(row, 'approve')"
              >通过</el-button>
              <el-button size="small" data-queue-action="reject" @click="doReject(row)">驳回</el-button>
            </template>
            <el-button
              v-if="g.key === 'failed'"
              size="small"
              data-queue-action="retry"
              :disabled="inflight"
              @click="doRetry(row)"
            >重试</el-button>
            <a class="queue-row-link" data-queue-action="logs" :href="`${shellBase()}/data/logs`">看台账</a>
            <el-button size="small" data-queue-action="detail" @click="openDetail(row)">详情</el-button>
          </span>
        </li>
      </ul>
    </section>

    <ObjectDrawer :target="drawer.target" @close="closeDrawer_" @action="onDrawerAction" />
  </div>
</template>

<style scoped>
/* 设计语言（Adminator 参照，只作用于本页新增组件）：组标签 = nav-label 式小字弱色；
   行 hover = --bg-hover；徽标 = soft 描边胶囊；间距取 4-20 紧凑档。
   不动 app.css 全局令牌；不限宽（无内容列 max-width）。 */
.queue {
  display: grid;
  gap: 16px;
}
.queue-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 16px;
  padding: 10px 16px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  font-size: 13px;
  color: var(--t-sub);
}
.queue-summary-date {
  font-weight: 700;
  color: var(--t-base);
}
.queue-summary-item.is-bad {
  color: var(--danger);
}
.queue-summary-loading {
  color: var(--t-muted);
}
.queue-group {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  padding: 12px 16px;
}
.queue-group-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}
.queue-group-label {
  font-size: var(--fs-label);
  font-weight: 600;
  color: var(--t-muted);
}
.queue-group-count {
  font-size: var(--fs-label);
  color: var(--t-weak);
  font-variant-numeric: tabular-nums;
}
.queue-group-loading {
  font-size: 12px;
  color: var(--t-muted);
}
.queue-group-pages {
  margin-left: auto;
}
.queue-empty {
  margin: 8px 0;
  font-size: 13px;
  color: var(--t-muted);
}
.queue-rows {
  margin: 0;
  padding: 0;
  list-style: none;
}
.queue-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 8px;
  margin: 0 -8px;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
}
.queue-row:hover {
  background: var(--bg-hover);
}
.queue-row-name {
  flex: none;
  width: 160px;
  color: var(--t-base);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue-row-phone {
  flex: none;
  width: 110px;
  color: var(--t-sub);
  font-variant-numeric: tabular-nums;
}
.queue-row-model {
  flex: 1 1 120px;
  min-width: 0;
  color: var(--t-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue-row-state {
  flex: none;
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-soft);
  color: var(--t-sub);
}
.queue-row-state.is-warn {
  color: var(--warning);
  border-color: var(--warning);
}
.queue-row-state.is-bad {
  color: var(--danger);
  border-color: var(--danger);
}
.queue-row-msg {
  flex: 1 1 160px;
  min-width: 0;
  color: var(--t-muted);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue-row-actions {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
.queue-row-link {
  font-size: 12px;
  color: var(--primary);
  white-space: nowrap;
}
.mono {
  font-family: var(--font-mono);
  font-size: 12px;
}
/* 窄屏：次要列（机型 / 消息）收起，动作保持可达 */
@media (max-width: 720px) {
  .queue-row-name { width: 120px; }
  .queue-row-model { display: none; }
  .queue-row-msg { flex: 1 1 auto; }
}
</style>
