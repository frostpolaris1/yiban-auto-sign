<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, maskEmail, openPasswordModal, passwordHint, shellBase, swapOut, toast } from "../lib/shell";
import "./ops.js"; // 注册 window.YB.userOps（MF-49 出口面：纯 JS，由 Python 侧 node 真跑钉住）
import {
  GROUPS,
  batchActions,
  countLabel,
  deletedBadge,
  groupSpec,
  inlineMeta,
  listOf,
  mapDeleted,
  mapUsers,
  matches,
  menuActions,
  remainText,
  roleBadge,
  type ActionKey,
  type DeletedRecord,
  type Group,
  type UserRecord,
} from "./model";

/* 用户管理（管理端 /work/users）。

   与 legacy 的分工：写操作链路在 `ops.js`（纯 JS，被 MF-49 出口面真跑钉住：单条端点按
   不透明 id 编 path、邮箱只进 batch/purge 请求体、id 缺失拒绝发请求），分组/检索/计数/
   行菜单/展示文案在 `model.ts`（纯函数 + 单测），本组件只做编排与渲染。

   ## 本页的安全硬约束（照抄 legacy，逐条都在）
   1. `/api/users` 返回**完整邮箱**，但只在 JS 内存态持有（选择态用邮箱作键，键不进 DOM）；
      渲染一律走外壳的 `maskEmail`（core.js 唯一实现，与后端真跑对拍）——完整邮箱不落任何
      DOM 文本或属性（不落 `data-*`、不落 `title`；`title` 只放时间字符串）。
   2. 单条操作从记录回查**不透明 id** 编进 URL path；邮箱只在 batch/purge 的请求体里
      （MF-49：path 进 nginx `$request`、经同源 Referrer 外送）。
   3. 零 `v-html`：全部走插值/属性绑定。
   4. 不做轮询（用户列表变化低频）；刷新只由写操作成功后就地重拉触发。

   ## 与 legacy 的三处有意简化
   · 四组共用一个 `v-for` + 列定义表（legacy 是四份近重复模板 + 四份近乎同构的 JS），差异
     （列、计数表头、内置行、批量动作、空态出口）全部落在 `model.ts::GROUPS`；
   · 行菜单用 `el-dropdown`（legacy 借共享的 `row-menu.js` + 手工 portal + closeAll）——
     那套是给多页复用的，本页迁完不必再借；`row-menu.js` 仍留给账号页与设置页；
   · **不再使用 core.js 的页签契约**（`data-tab-group`/`data-tab-target`/`data-tab-id`）：
     那套会与本组件的 `is-active` 双向争抢 DOM（core.js 有 document 级点击委托）。
     故页签由本组件自管（含 `?tab=` 深链与 roving tabindex），并因此从
     `tests/test_web_page_consistency.py::ADMIN_PAGES` 移出——其判据是"服务端渲染的初始
     ARIA 结构"，本页已改为客户端渲染（同 data_logs 的处置）。 */
const ANIM_MIN_MS = 80;
const TAB_KEYS: Group[] = ["pending", "normal", "vacant", "deleted"];

interface UserOps {
  role(uid: string, r: string): void;
  resetPassword(uid: string, pw: string): void;
  deleteUser(uid: string, mode: string): void;
  purge(uid: string): void;
  batchReset(uids: string[], pw: string): void;
  batchDelete(uids: string[]): void;
  batchPurge(uids: string[]): void;
}

const users = ref<UserRecord[]>([]);
const deleted = ref<DeletedRecord[]>([]);
const builtin = ref("admin");
const isMaster = ref(false);
const loading = ref(true);
const loadError = ref(false);
const tab = ref<Group>("pending");
const search = ref<Record<string, string>>({ pending: "", normal: "", vacant: "", deleted: "" });
/** 选择态：按组持有邮箱（仅内存；键不进 DOM） */
const sel = ref<Record<string, string[]>>({ pending: [], normal: [], vacant: [], deleted: [] });

const rootEl = ref<HTMLElement | null>(null);

/** 已注销整组无数据时连标签一起收掉（否则点进去是一片空） */
const visibleGroups = computed(() => GROUPS.filter((g) => g.key !== "deleted" || deleted.value.length > 0));

function specOf(group: Group): ReturnType<typeof groupSpec> {
  return groupSpec(group);
}
function userRows(group: Group): UserRecord[] {
  if (group === "deleted") return [];
  const kw = search.value[group] ?? "";
  return listOf(users.value, group).filter((u) => matches(u.email, maskEmail(u.email), kw));
}
function rowsOf(group: Group): { email: string }[] {
  return group === "deleted" ? deleted.value : userRows(group);
}
function totalOf(group: Group): number {
  if (group === "deleted") return deleted.value.length;
  const base = listOf(users.value, group).length;
  return group === "normal" ? base + 1 : base; // 正式组首行固定为内置主管理员
}
function shownOf(group: Group): number {
  if (group === "deleted") return deleted.value.length;
  const base = userRows(group).length;
  // 内置主管理员行也受搜索约束，故这里必须问 showBuiltin（只 +1 会让筛到 0 条时
  // 「可见行数 == 计数」不成立，空态与「无匹配结果」都不会出现）
  return group === "normal" ? base + (showBuiltin(group) ? 1 : 0) : base;
}
/** 主管理员行同样受搜索约束：写死显示会让「可见行数 == 计数」不成立（空态也会误判） */
function showBuiltin(group: Group): boolean {
  return group === "normal" && matches(builtin.value, maskEmail(builtin.value), search.value[group] ?? "");
}
function countText(group: Group): string {
  return countLabel(totalOf(group), shownOf(group), search.value[group] ?? "");
}
/** 真·空 vs 筛出来的空：前者给「下一步」，后者给「清除筛选」 */
function isFiltering(group: Group): boolean {
  return group !== "deleted" && listOf(users.value, group).length > 0 && userRows(group).length === 0;
}
function emptyText(group: Group): string {
  if (shownOf(group) > 0) return "";
  return isFiltering(group) ? "无匹配结果" : specOf(group).emptyText;
}

/* ---------------- 选择态 ---------------- */
function isSelected(group: Group, email: string): boolean {
  return sel.value[group].includes(email);
}
function toggleRow(group: Group, email: string, on: boolean): void {
  const cur = sel.value[group];
  sel.value = { ...sel.value, [group]: on ? [...cur, email] : cur.filter((e) => e !== email) };
}
function selectAllState(group: Group): { checked: boolean; indeterminate: boolean } {
  const rows = rowsOf(group);
  const picked = rows.filter((r) => isSelected(group, r.email)).length;
  return { checked: rows.length > 0 && picked === rows.length, indeterminate: picked > 0 && picked < rows.length };
}
function onSelectAll(group: Group, on: boolean): void {
  sel.value = { ...sel.value, [group]: on ? rowsOf(group).map((r) => r.email) : [] };
}
function clearSelection(group: Group): void {
  sel.value = { ...sel.value, [group]: [] };
}
function selCount(group: Group): number {
  return sel.value[group].length;
}
/** 数据刷新后清掉已不存在的选中项（否则批量会带上已删除的用户） */
function pruneSelection(): void {
  const live = new Set([...users.value, ...deleted.value].map((r) => r.email));
  const next: Record<string, string[]> = {};
  for (const g of Object.keys(sel.value)) next[g] = sel.value[g].filter((e) => live.has(e));
  sel.value = next;
}

/* ---------------- 数据加载 ---------------- */
function makeOps(): UserOps {
  const shell = (window as { YB?: { userOps?: { create(c: unknown): unknown } } }).YB;
  return (shell?.userOps?.create({
    busy: () => {
      /* 本页无轮询与并发重建，忙碌态不改变视图 */
    },
    refresh: () => load(true),
    resolve: (email: string) =>
      users.value.find((u) => u.email === email) ?? deleted.value.find((d) => d.email === email),
  }) ?? {}) as UserOps;
}

async function load(animate = false): Promise<void> {
  const t0 = performance.now();
  const [u, d] = await Promise.all([api("GET", "/api/users"), api("GET", "/api/users/deleted")]);
  const apply = (): void => {
    const mapped = mapUsers(u);
    users.value = mapped.users;
    builtin.value = mapped.builtin;
    deleted.value = mapDeleted(d);
    pruneSelection();
    loading.value = false;
    loadError.value = false;
    if (!deleted.value.length && tab.value === "deleted") tab.value = "pending";
  };
  // 写操作后的慢刷新：整体淡出 → 换内容 → 淡入（快请求不播，<80ms 只会闪一下）
  if (animate && performance.now() - t0 > ANIM_MIN_MS && rootEl.value) swapOut(rootEl.value, apply);
  else apply();
}

async function startLoad(): Promise<void> {
  loading.value = true;
  loadError.value = false;
  try {
    await load(false);
  } catch (e) {
    // 接口失败时四张表都可能空白：显式标「加载失败」，避免被误读成「没有用户」
    loading.value = false;
    loadError.value = true;
    toast().error((e as Error)?.message || "加载用户列表失败，请稍后重试");
  }
}

/* ---------------- 操作派发 ---------------- */
function onRowAction(row: UserRecord, key: ActionKey): void {
  const o = makeOps();
  const uid = row.email;
  if (key === "grant_admin") o.role(uid, "admin");
  else if (key === "revoke_admin") o.role(uid, "user");
  else if (key === "reset_password") askNewPassword((pw) => o.resetPassword(uid, pw));
  else if (key === "clear_accounts") o.deleteUser(uid, "accounts_only");
  else if (key === "delete_user") o.deleteUser(uid, "full");
}

/** 重置口令入口：统一口径提示交给共享密码模态（长度 + 类别判定在模态内完成，本页不自带策略） */
function askNewPassword(cb: (pw: string) => void): void {
  openPasswordModal(`设置新密码（${passwordHint(false)}）。重置后该用户的旧会话立即失效。`, cb);
}

function onBatch(group: Group, action: string): void {
  const picked = sel.value[group];
  if (!picked.length) return;
  const o = makeOps();
  if (action === "reset_password") askNewPassword((pw) => o.batchReset(picked, pw));
  else if (action === "delete") o.batchDelete(picked);
  else if (action === "purge") o.batchPurge(picked);
}

/* ---------------- 页签 ---------------- */
function cellClass(key: string): string {
  const map: Record<string, string> = {
    check: "usr-cell-check",
    mail: "usr-cell-mail",
    role: "usr-cell-role",
    count: "usr-cell-count",
    time: "usr-cell-time",
    deleted_at: "usr-cell-time",
    remain: "usr-cell-remain",
    status: "usr-cell-status",
    actions: "usr-cell-actions",
  };
  return map[key] ?? "";
}
function syncTabUrl(next: Group): void {
  try {
    const params = new URLSearchParams(location.search);
    params.set("tab", next);
    history.replaceState(null, "", `${location.pathname}?${params.toString()}${location.hash}`);
  } catch {
    /* 无 history 的环境静默降级 */
  }
}
watch(tab, syncTabUrl);

onMounted(async () => {
  const wanted = new URLSearchParams(location.search).get("tab");
  if (wanted && (TAB_KEYS as string[]).includes(wanted)) tab.value = wanted as Group;
  try {
    const me = await api<{ is_builtin_admin?: boolean }>("GET", "/api/me");
    isMaster.value = !!me?.is_builtin_admin;
  } catch {
    window.location.href = shellBase() + "/login";
    return;
  }
  await startLoad();
});
</script>

<template>
  <div class="users-page">
    <div class="page-head">
      <h1 class="page-title">用户管理</h1>
      <p class="page-sub">管理注册用户与管理员权限；密码仅可重置，不可查看。</p>
    </div>

    <!-- 页面级状态条：加载中 / 加载失败（含重试）。接口失败时四张表可能全空，没有这条会被
         误读成「没有用户」；成功即隐藏。 -->
    <p
      id="usr-status"
      class="alert"
      :class="loadError ? 'danger' : 'info'"
      role="status"
      :hidden="!loading && !loadError"
    >
      <span class="ico"><svg aria-hidden="true"><use href="#i-clock" /></svg></span>
      <span class="body" id="usr-status-text">{{ loadError ? "用户列表加载失败" : "正在加载用户列表…" }}</span>
      <button type="button" class="btn btn--ghost btn--sm" data-usr-retry :hidden="!loadError" @click="startLoad">
        重试
      </button>
    </p>

    <div id="users-root" ref="rootEl">
      <div class="usr-tabs-root">
        <div class="tabs-scroll">
          <div class="tabs" role="tablist" aria-label="用户管理分区">
            <!-- 计数放在标签上：切标签前就能看到各组的量（roving tabindex 由本组件维护） -->
            <a
              v-for="g in visibleGroups"
              :key="g.key"
              class="tab"
              :class="{ 'is-active': tab === g.key }"
              role="tab"
              :id="'usr-tab-' + g.key"
              :aria-selected="tab === g.key ? 'true' : 'false'"
              :aria-controls="'usr-panel-' + g.key"
              href="#"
              :data-usr-tab="g.key"
              :tabindex="tab === g.key ? 0 : -1"
              @click.prevent="tab = g.key"
            >
              {{ g.title }} <span class="usr-count" :id="'usr-' + g.key + '-count'">{{ countText(g.key) }}</span>
            </a>
          </div>
        </div>

        <div
          v-for="g in visibleGroups"
          :key="g.key"
          class="tab-panel"
          :class="{ 'is-active': tab === g.key }"
          role="tabpanel"
          :id="'usr-panel-' + g.key"
          :aria-labelledby="'usr-tab-' + g.key"
          :data-usr-panel="g.key"
        >
          <section class="card" :id="'usr-' + g.key + '-card'">
            <div class="panel-head usr-group-head">
              <div class="panel-head-row">
                <h2 class="panel-title">{{ g.title }}</h2>
                <label v-if="g.searchable" class="usr-search">
                  <span class="sr-only">搜索{{ g.title }}</span>
                  <input
                    v-model="search[g.key]"
                    type="search"
                    class="input"
                    :id="'usr-' + g.key + '-search'"
                    placeholder="搜索邮箱"
                    autocomplete="off"
                  />
                </label>
              </div>
              <p class="panel-sub">{{ g.sub }}</p>
            </div>

            <div :id="'usr-' + g.key + '-body'" class="collapse-body is-open">
              <div class="collapse-inner">
                <!-- 批量条：有选中才显形；动作集由组定义表给出 -->
                <div
                  class="usr-batch"
                  :id="'usr-batch-' + g.key"
                  role="status"
                  aria-live="polite"
                  :hidden="selCount(g.key) === 0"
                >
                  <span class="usr-batch-count">
                    已选 <strong :id="'usr-batch-count-' + g.key">{{ selCount(g.key) }}</strong> 个
                  </span>
                  <div class="usr-batch-actions">
                    <button
                      v-for="a in batchActions(g.key)"
                      :key="a.key"
                      type="button"
                      class="btn btn--sm"
                      :class="a.variant"
                      :data-usr-batch="g.key + ':' + a.key"
                      :hidden="a.key === 'purge' && !isMaster"
                      @click="onBatch(g.key, a.key)"
                    >
                      {{ a.label }}
                    </button>
                    <button
                      type="button"
                      class="btn btn--ghost btn--sm"
                      :data-usr-batch-clear="g.key"
                      @click="clearSelection(g.key)"
                    >
                      取消选择
                    </button>
                  </div>
                </div>

                <div class="table-scroll">
                  <table class="data-table usr-table" :aria-label="g.title">
                    <thead>
                      <tr>
                        <th v-for="col in g.columns" :key="col.key" scope="col" :class="[cellClass(col.key), col.narrowHidden ? 'usr-col-md' : '']">
                          <label v-if="col.key === 'check'" class="usr-check">
                            <input
                              type="checkbox"
                              :id="'usr-select-all-' + g.key"
                              :aria-label="'全选' + g.title"
                              :checked="selectAllState(g.key).checked"
                              :indeterminate="selectAllState(g.key).indeterminate"
                              @change="onSelectAll(g.key, ($event.target as HTMLInputElement).checked)"
                            />
                          </label>
                          <template v-else>{{ col.label }}</template>
                        </th>
                      </tr>
                    </thead>
                    <tbody :id="'usr-' + g.key + '-tbody'">
                      <template v-if="loading">
                        <tr v-for="i in 3" :key="'sk' + i" class="skel-row" aria-hidden="true">
                          <td :colspan="g.columns.length"><span class="skeleton skeleton--text"></span></td>
                        </tr>
                      </template>

                      <!-- 已注销组：另一套列与徽标 -->
                      <template v-else-if="g.key === 'deleted'">
                        <tr v-for="row in deleted" :key="'d:' + row.email">
                          <td class="usr-cell-check">
                            <label class="usr-check">
                              <input
                                type="checkbox"
                                :aria-label="'选择用户 ' + maskEmail(row.email)"
                                :checked="isSelected('deleted', row.email)"
                                @change="toggleRow('deleted', row.email, ($event.target as HTMLInputElement).checked)"
                              />
                            </label>
                          </td>
                          <td class="usr-cell-mail" :title="row.deleted_at">{{ maskEmail(row.email) }}</td>
                          <td class="usr-cell-time usr-col-md">{{ row.deleted_at || "—" }}</td>
                          <td class="usr-cell-remain usr-col-md">{{ remainText(row.remaining_days, row.status) }}</td>
                          <td class="usr-cell-status">
                            <span class="badge" :class="'badge--' + deletedBadge(row.status).tone">
                              {{ deletedBadge(row.status).label }}
                            </span>
                          </td>
                          <td class="usr-cell-actions">
                            <el-dropdown v-if="isMaster" trigger="click">
                              <button type="button" class="btn btn--ghost btn--sm" :aria-label="'更多操作 ' + maskEmail(row.email)">
                                更多操作
                              </button>
                              <template #dropdown>
                                <el-dropdown-menu>
                                  <el-dropdown-item @click="makeOps().purge(row.email)">立即清除</el-dropdown-item>
                                </el-dropdown-menu>
                              </template>
                            </el-dropdown>
                            <span v-else class="usr-muted">仅主管理员可清除</span>
                          </td>
                        </tr>
                      </template>

                      <!-- 其余三组：共用一套列渲染 -->
                      <template v-else>
                        <!-- 内置主管理员行：不可选、不可改（只有正式用户组有） -->
                        <tr v-if="showBuiltin(g.key)" class="usr-row-master">
                          <td class="usr-cell-check"></td>
                          <td class="usr-cell-mail">
                            {{ maskEmail(builtin) }}<span class="usr-muted">（主管理员）</span>
                          </td>
                          <td class="usr-cell-role"><span class="badge badge--info">管理员</span></td>
                          <td v-if="g.key === 'normal'" class="usr-cell-count usr-col-md">—</td>
                          <td class="usr-cell-time usr-col-md">—</td>
                          <td class="usr-cell-actions"><span class="usr-muted">不可改</span></td>
                        </tr>
                        <tr v-for="row in userRows(g.key)" :key="'u:' + row.email">
                          <td class="usr-cell-check">
                            <label class="usr-check">
                              <input
                                type="checkbox"
                                :aria-label="'选择用户 ' + maskEmail(row.email)"
                                :checked="isSelected(g.key, row.email)"
                                @change="toggleRow(g.key, row.email, ($event.target as HTMLInputElement).checked)"
                              />
                            </label>
                          </td>
                          <td class="usr-cell-mail" :title="row.created_at">
                            {{ maskEmail(row.email) }}
                            <span v-if="inlineMeta(row, g.key)" class="usr-inline-meta">{{ inlineMeta(row, g.key) }}</span>
                          </td>
                          <td class="usr-cell-role">
                            <span class="badge" :class="'badge--' + roleBadge(row.role).tone">{{ roleBadge(row.role).label }}</span>
                          </td>
                          <td v-if="g.key === 'pending'" class="usr-cell-count usr-col-md">{{ row.review_count }}</td>
                          <td v-else-if="g.key === 'normal'" class="usr-cell-count usr-col-md">{{ row.account_count }}</td>
                          <td class="usr-cell-time usr-col-md">{{ row.created_at || "—" }}</td>
                          <td class="usr-cell-actions">
                            <!-- 行菜单由 model 给出：非主管理员对注册管理员目标不给任何动作
                                 （后端 403 兜底；UI 只是不给出不可能成功的动作） -->
                            <el-dropdown v-if="menuActions(row, g.key, isMaster).length" trigger="click">
                              <button type="button" class="btn btn--ghost btn--sm" :aria-label="'更多操作 ' + maskEmail(row.email)">
                                更多操作
                              </button>
                              <template #dropdown>
                                <el-dropdown-menu>
                                  <el-dropdown-item
                                    v-for="a in menuActions(row, g.key, isMaster)"
                                    :key="a.key"
                                    :class="{ 'is-danger': a.danger }"
                                    @click="onRowAction(row, a.key)"
                                  >
                                    {{ a.label }}
                                  </el-dropdown-item>
                                </el-dropdown-menu>
                              </template>
                            </el-dropdown>
                            <span v-else class="usr-muted">仅主管理员可操作</span>
                          </td>
                        </tr>
                      </template>
                    </tbody>
                  </table>
                </div>

                <!-- 空态：真·空给「下一步」（跨分组跳转）；筛出来的空给「清除筛选」
                     （原来这里直接把按钮藏掉，筛到 0 条时整块面板一个按钮都没有） -->
                <p class="empty" :id="'usr-' + g.key + '-empty'" :hidden="shownOf(g.key) > 0 && !loadError">
                  <span class="empty__icon"><svg aria-hidden="true"><use href="#i-users" /></svg></span>
                  <span class="empty__msg">{{ loadError ? "加载失败" : emptyText(g.key) }}</span>
                  <span class="empty__action" :hidden="loadError">
                    <button
                      v-if="isFiltering(g.key)"
                      type="button"
                      class="btn btn--ghost btn--sm"
                      :data-empty-clear="g.key"
                      @click="search[g.key] = ''"
                    >
                      清除筛选
                    </button>
                    <button
                      v-else-if="g.emptyAction"
                      type="button"
                      class="btn btn--ghost btn--sm"
                      :data-empty-tab="g.emptyAction.tab"
                      @click="tab = g.emptyAction.tab"
                    >
                      {{ g.emptyAction.label }}
                    </button>
                  </span>
                </p>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>
