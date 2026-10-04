<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ApiError, apiGet } from "../lib/api";
import {
  DEFAULT_PAGE_SIZE,
  EMPTY_FILTERS,
  buildAuditQuery,
  clampPageSize,
  type AuditFilters,
} from "./query";
import {
  canLoadMore,
  mergePage,
  nextPageNumber,
  resetState,
  type AuditPageResponse,
  type AuditState,
} from "./paging";

/* 审计日志页（GET /api/audit-logs）。
   纪律：
   · 后端已做脱敏单出口（username→actor_tag、target/detail→_nl_safe 截断），
     前端**不得**再加工、也不得用 v-html —— 一律插值（等价 textContent）；
   · 翻页终止只看 has_more（见 paging.ts 注释）；
   · 行定位用不透明 row_id 作 key，界面不展示、也不构造任何自增 id。 */

const filters = ref<AuditFilters>({ ...EMPTY_FILTERS });
const pageSize = ref(DEFAULT_PAGE_SIZE);
const state = ref<AuditState>(resetState());
const loading = ref(false);
const errorText = ref("");

const rows = computed(() => state.value.rows);
const total = computed(() => state.value.total);
const loadMoreAvailable = computed(() => canLoadMore(state.value));

async function fetchPage(page: number, append: boolean): Promise<void> {
  loading.value = true;
  errorText.value = "";
  try {
    const resp = await apiGet<AuditPageResponse>(buildAuditQuery(filters.value, page, pageSize.value));
    state.value = mergePage(state.value, resp, append);
  } catch (err) {
    errorText.value = err instanceof ApiError ? err.message : "网络错误，请重试";
  } finally {
    loading.value = false;
  }
}

/** 首屏与「查询」：从第 1 页整表替换。 */
function search(): void {
  void fetchPage(1, false);
}

/** 加载更多：追加下一页（页码由 state 推导，避免重复点击翻两页）。 */
function loadMore(): void {
  if (loading.value || !loadMoreAvailable.value) return;
  void fetchPage(nextPageNumber(state.value), true);
}

function resetFilters(): void {
  filters.value = { ...EMPTY_FILTERS };
  search();
}

function onPageSizeChange(): void {
  pageSize.value = clampPageSize(pageSize.value);
  search();
}

onMounted(search);
</script>

<template>
  <div class="audit">
    <section class="card">
      <div class="audit-toolbar">
        <label class="audit-field">
          <span class="audit-label">动作</span>
          <input v-model="filters.action" class="input" type="text" placeholder="如 login_success" @keyup.enter="search" />
        </label>
        <label class="audit-field">
          <span class="audit-label">操作者</span>
          <input v-model="filters.actor" class="input" type="text" placeholder="用户名（服务端按遮罩口径匹配）" @keyup.enter="search" />
        </label>
        <label class="audit-field">
          <span class="audit-label">对象</span>
          <input v-model="filters.target" class="input" type="text" placeholder="如账号 / 用户标识" @keyup.enter="search" />
        </label>
        <label class="audit-field">
          <span class="audit-label">起止日期</span>
          <span class="audit-dates">
            <input v-model="filters.fromDate" class="input" type="date" />
            <span class="audit-dash">–</span>
            <input v-model="filters.toDate" class="input" type="date" />
          </span>
        </label>
        <div class="audit-actions">
          <button type="button" class="btn btn--primary" :disabled="loading" @click="search">查询</button>
          <button type="button" class="btn btn--ghost" :disabled="loading" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="audit-head">
        <h2 class="audit-title">审计记录</h2>
        <span class="audit-meta">
          共 <strong>{{ total }}</strong> 条<template v-if="rows.length">（已显示 {{ rows.length }}）</template>
        </span>
        <label class="audit-pagesize">
          <span class="audit-label">每页</span>
          <select v-model.number="pageSize" class="input" @change="onPageSizeChange">
            <option :value="50">50</option>
            <option :value="100">100</option>
            <option :value="200">200</option>
          </select>
        </label>
      </div>

      <el-alert
        v-if="errorText"
        type="error"
        :title="errorText"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
      />
      <el-skeleton v-if="loading && !rows.length" :rows="6" animated />
      <el-table v-else :data="rows" size="small" row-key="row_id" empty-text="没有匹配的审计记录">
        <el-table-column prop="ts" label="时间" width="170" />
        <el-table-column prop="actor" label="操作者" width="150" />
        <el-table-column prop="action" label="动作" width="180" />
        <el-table-column prop="target" label="对象" min-width="160" show-overflow-tooltip />
        <el-table-column prop="detail" label="详情" min-width="240" show-overflow-tooltip />
      </el-table>

      <div v-if="rows.length" class="audit-foot">
        <button type="button" class="btn btn--ghost" :disabled="loading || !loadMoreAvailable" @click="loadMore">
          {{ loadMoreAvailable ? "加载更多" : "已到末尾" }}
        </button>
        <span v-if="loading" class="audit-meta">加载中…</span>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 卡片外观**不在此定义**：直接复用设计系统的全局 `.card`（adminator.css 提供底/边/圆角/内距，
   app.css 有密度变体）——局部重定义会与全局规则叠加，正是「两套样式层」的老问题。
   这里只写本页专有的排布类。 */
.audit {
  display: grid;
  gap: 16px;
}
.audit-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
}
.audit-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.audit-label {
  font-size: 12px;
  color: var(--t-muted);
}
.audit-dates {
  display: flex;
  align-items: center;
  gap: 6px;
}
.audit-dash {
  color: var(--t-light);
}
.audit-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
.audit-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.audit-title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--t-base);
}
.audit-meta {
  font-size: 12.5px;
  color: var(--t-muted);
}
.audit-pagesize {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
.audit-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
}
</style>
