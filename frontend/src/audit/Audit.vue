<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, errorMessage } from "../lib/shell";
import {
  DEFAULT_PAGE_SIZE,
  EMPTY_FILTERS,
  buildAuditQuery,
  clampPageSize,
  type AuditFilters,
} from "./query";
import {
  mergePage,
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

async function fetchPage(page: number, append: boolean): Promise<void> {
  loading.value = true;
  errorText.value = "";
  try {
    const resp = await api<AuditPageResponse>(buildAuditQuery(filters.value, page, pageSize.value));
    state.value = mergePage(state.value, resp, append);
  } catch (err) {
    errorText.value = errorMessage(err, "网络错误，请重试");
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

/** 翻页：整页替换（与账号/用户/日志页的 el-pagination 同形，不再用"加载更多"追加）。 */
function goPage(page: number): void {
  if (loading.value || page === state.value.page) return;
  void fetchPage(page, false);
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
          <button type="submit" class="btn btn--primary btn--sm" :disabled="loading">查询</button>
          <button type="button" class="btn btn--ghost btn--sm" :disabled="loading" @click="resetFilters">重置</button>
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
      <!-- 窄屏（≤900）用卡片列表：5 列固定宽合计约 900px，在 390/360 只能看到前两列，
           对象与详情既看不见、也没有 hover 可用的 tooltip。桌面仍用 el-table。 -->
      <template v-else>
        <div class="audit-table-wrap">
          <el-table :data="rows" size="small" row-key="row_id" empty-text="没有匹配的审计记录">
            <el-table-column prop="ts" label="时间" width="170" />
            <el-table-column prop="actor" label="操作者" width="150" />
            <el-table-column prop="action" label="动作" width="180" />
            <el-table-column prop="target" label="对象" min-width="160" show-overflow-tooltip />
            <el-table-column prop="detail" label="详情" min-width="240" show-overflow-tooltip />
          </el-table>
        </div>
        <div v-if="rows.length" class="audit-cards" aria-label="审计记录">
          <article v-for="row in rows" :key="'card:' + row.row_id" class="audit-card">
            <div class="audit-card-top">
              <span class="audit-card-time">{{ row.ts }}</span>
              <span class="audit-card-action">{{ row.action }}</span>
            </div>
            <div class="audit-card-line"><span>操作者</span>{{ row.actor }}</div>
            <div v-if="row.target" class="audit-card-line"><span>对象</span>{{ row.target }}</div>
            <p v-if="row.detail" class="audit-card-detail">{{ row.detail }}</p>
          </article>
        </div>
        <p v-else class="audit-cards-empty">没有匹配的审计记录</p>
      </template>

      <div v-if="total > 0" class="audit-foot">
        <el-pagination
          :current-page="state.page"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          background
          @current-change="goPage"
        />
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
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr)) minmax(0, 1.55fr) auto;
  align-items: end;
  gap: 12px;
}
.audit-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  width: 100%;
}
.audit-field .input { width: 100%; min-width: 0; padding: 4px 10px; }
.audit-label {
  font-size: 12px;
  color: var(--t-muted);
}
.audit-dates {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: 6px;
  min-width: 0;
}
/* 窄屏（≤720px，与全站窄屏段同口径）：字段单列，两个日期框各占一半宽度。
   （工单 yiban-auto-sign-w26p：360px 实测越界 18px）。 */
@media (max-width: 720px) {
  .audit-toolbar { grid-template-columns: minmax(0, 1fr); }
  .audit-actions .btn { flex: 1 1 0; }
}
@media (min-width: 721px) and (max-width: 1100px) {
  .audit-toolbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  /* 两列档里「对象」与日期各自独占整行：日期两框在 ~250px 列内会被压到
     看不清完整日期；整行后每框仍有 ~240px。 */
  .audit-field:nth-child(3),
  .audit-field:nth-child(4),
  .audit-actions { grid-column: 1 / -1; }
}
.audit-dash {
  color: var(--t-weak);
}
/* 日期选择器宽度跟随网格列：EP 的 `.el-date-editor.el-input`（0,2,0）会把宽度锁在
   220px；且 class 落在 EP 子组件根上、不带本组件的 data-v 属性，故必须用 `:deep`。
   选择器因此是 `.audit-dates[data-v] .audit-date`（0,3,0）。 */
.audit-dates :deep(.audit-date) {
  width: 100%;
  min-width: 0;
}
.audit-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
.audit-head {
  display: flex;
  flex-wrap: wrap;
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

/* 窄屏审计记录：桌面表格 5 列合计约 900px，容器不足 900px 时只能看到前两列，
   且对象/详情依赖 hover tooltip（触屏不可达）。≤1100 已放不下整表，改为逐条卡片。 */
.audit-cards { display: none; }
.audit-cards-empty { display: none; }
@media (max-width: 1100px) {
  .audit-table-wrap { display: none; }
  .audit-cards {
    display: grid;
    gap: 10px;
  }
  .audit-cards-empty {
    display: block;
    margin: 20px 0;
    text-align: center;
    font-size: 12px;
    color: var(--t-muted);
  }
  .audit-card {
    min-width: 0;
    padding: 12px;
    border: 1px solid var(--border-soft);
    border-radius: 10px;
    background: var(--bg-card);
  }
  .audit-card-top {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
  }
  .audit-card-time {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--t-muted);
    font-variant-numeric: tabular-nums;
  }
  .audit-card-action {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--t-base);
    overflow-wrap: anywhere;
    text-align: right;
  }
  .audit-card-line {
    margin-top: 6px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--t-sub);
    overflow-wrap: anywhere;
  }
  .audit-card-line > span {
    margin-right: 6px;
    color: var(--t-muted);
  }
  .audit-card-detail {
    margin: 8px 0 0;
    padding-top: 8px;
    border-top: 1px solid var(--border-soft);
    font-size: 12px;
    line-height: 1.6;
    color: var(--t-sub);
    overflow-wrap: anywhere;
  }
}
</style>
