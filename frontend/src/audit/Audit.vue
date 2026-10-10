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

/**
 * 组合输入（中文输入法）进行中。
 *
 * 为什么需要一个显式标志：组合期的那次回车是「提交候选词」，浏览器**仍会对表单做隐式提交**
 * （实测 Chromium：keydown 的 isComposing 为真时 submit 照样发生一次）。只在 keydown 里
 * `if (isComposing) return` 挡不住它，提交照样进 search()，发出去的是**未提交**的拼音串。
 * 故把守卫放在提交路径上，并保证组合期不 preventDefault（不打断输入法）。
 */
const composing = ref(false);

/**
 * 表单提交（点「查询」，以及回车走到的那次隐式提交）。
 *
 * 组合期直接返回：这次提交是输入法提交候选词带来的，不是检索指令。
 * 注意 `.prevent` 由模板编译器内联在处理器之前执行，所以这里早退也不会发生原生 GET 重载。
 */
function submitSearch(): void {
  if (composing.value) return;
  search();
}

/**
 * 输入框里按下回车。
 *
 * 组合期交给输入法（直接返回，也不 preventDefault）；非组合期挡掉隐式提交再发起查询——
 * 挡掉是为了让一次回车只发一次请求（否则 keydown 与隐式提交会各发一次）。
 */
function onSearchEnter(e: KeyboardEvent): void {
  if (e.isComposing || composing.value) return;
  e.preventDefault();
  search();
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

/* el-date-picker 清空时回 null；归一成 "" 与旧原生 input 的空值口径一致
   （query.ts 的 toDayStart/toDayEnd 只认 "YYYY-MM-DD"，空串不下发该过滤键）。 */
function setFromDate(v: string | null): void {
  filters.value.fromDate = v ?? "";
}
function setToDate(v: string | null): void {
  filters.value.toDate = v ?? "";
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
      <!-- 筛选是一段**表单**：移动端键盘的「搜索/前往」键只有落到表单提交才有去处。
           回车另有输入框上的 keydown 处理：查询按钮在 loading 期间是 disabled，而隐式提交
           要求默认按钮可用——只留表单会在 loading 窗口把回车变成静默无效。
           组合输入（中文输入法）期的回车不检索，由 form 上的 composing 标志挡住那次隐式提交
           （浏览器不为 isComposing 跳过隐式提交，只挡 keydown 处理器是不够的）。
           重置保持 type=button。 -->
      <form
        class="audit-toolbar"
        role="search"
        aria-label="审计筛选"
        @submit.prevent="submitSearch"
        @compositionstart="composing = true"
        @compositionend="composing = false"
      >
        <label class="audit-field">
          <span class="audit-label">动作</span>
          <input v-model="filters.action" class="input" type="text" placeholder="如 login_success" @keydown.enter="onSearchEnter" />
        </label>
        <label class="audit-field">
          <span class="audit-label">操作者</span>
          <input v-model="filters.actor" class="input" type="text" placeholder="用户名（服务端按遮罩口径匹配）" @keydown.enter="onSearchEnter" />
        </label>
        <label class="audit-field">
          <span class="audit-label">对象</span>
          <input v-model="filters.target" class="input" type="text" placeholder="如账号 / 用户标识" @keydown.enter="onSearchEnter" />
        </label>
        <label class="audit-field">
          <span class="audit-label">起止日期</span>
          <span class="audit-dates">
            <!-- 日期也走 Element Plus：原生 input[type=date] 的日历弹层由 UA 渲染、样式不可控。
                 value-format 保持 "YYYY-MM-DD"，与 query.ts 的 toDayStart/toDayEnd 口径逐字一致；
                 清空得到 null，由 setFromDate/setToDate 归一成 ""（等同旧原生空值）。 -->
            <el-date-picker
              :model-value="filters.fromDate || null"
              type="date"
              format="YYYY-MM-DD"
              value-format="YYYY-MM-DD"
              placeholder="开始"
              aria-label="开始日期"
              class="audit-date"
              @update:model-value="setFromDate"
            />
            <span class="audit-dash">–</span>
            <el-date-picker
              :model-value="filters.toDate || null"
              type="date"
              format="YYYY-MM-DD"
              value-format="YYYY-MM-DD"
              placeholder="结束"
              aria-label="结束日期"
              class="audit-date"
              @update:model-value="setToDate"
            />
          </span>
        </label>
        <div class="audit-actions">
          <button type="submit" class="btn btn--primary" :disabled="loading">查询</button>
          <button type="button" class="btn btn--ghost" :disabled="loading" @click="resetFilters">重置</button>
        </div>
      </form>
    </section>

    <section class="card">
      <div class="audit-head">
        <h2 class="audit-title">审计记录</h2>
        <span class="audit-meta">
          共 <strong>{{ total }}</strong> 条<template v-if="rows.length">（已显示 {{ rows.length }}）</template>
        </span>
        <div class="audit-pagesize">
          <span class="audit-label">每页</span>
          <!-- 选择控件一律走 Element Plus：原生 <select> 的下拉弹层由 UA 渲染、样式不可控
               （用户既定禁令）。data-select-field 是两栈共通的 e2e 锚点形态。
               可访问名用 aria-label：el-select 的 $attrs fallthrough 落在根 div（role=null），
               aria-labelledby 不会进 role=combobox 的内层 input；EP 会把 ariaLabel 转发到
               内层 input（select2.mjs 的 combobox 分支），故名字才成立。 -->
          <div class="select-field audit-pagesize-select" data-select-field="audit-pagesize">
            <el-select
              v-model.number="pageSize"
              aria-label="每页"
              style="width: 100%"
              @change="onPageSizeChange"
            >
              <el-option :value="50" label="50" />
              <el-option :value="100" label="100" />
              <el-option :value="200" label="200" />
            </el-select>
          </div>
        </div>
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
/* 窄屏（≤720px，与全站窄屏段同口径）：两个日期框各占一行，第二个框不再顶出视口
   （工单 yiban-auto-sign-w26p：360px 实测越界 18px）。
   换行只许在窄屏段：`.input` 的 width:100% 让 flex 基宽等于行宽，无条件 wrap 会把两个
   日期框在宽屏也压成两行（1280/1100/1024/900/768 实测两框分行、每框被拉到容器宽）。
   宽屏维持本批之前的同行布局。 */
@media (max-width: 720px) {
  .audit-dates {
    flex-wrap: wrap;
  }
}
.audit-dash {
  color: var(--t-weak);
}
/* 日期选择器宽度：EP .el-date-editor 默认 220px，两枚并排会过宽；收到与工具行同档的紧凑宽度。
   窄屏仍靠上面的 .audit-dates flex-wrap 规则换行。 */
.audit-date {
  width: 152px;
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
  font-size: var(--fs-title);
  font-weight: 700;
  color: var(--t-base);
}
.audit-meta {
  font-size: var(--fs-label);
  color: var(--t-muted);
}
.audit-pagesize {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
/* EP select 与本页原生 .input 同高（--ctl-h-lg = 40px）：EP 默认 32px。只在本页生效，
   不波及设置页的单点规则（.settings-page .el-select__wrapper）。 */
.audit-pagesize-select {
  width: 92px;
}
.audit-pagesize-select :deep(.el-select__wrapper) {
  min-height: 40px;
}
.audit-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
}
</style>
