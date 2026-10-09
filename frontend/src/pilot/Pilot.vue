<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { ApiError, apiGet } from "../lib/api";

/** GET /api/clock（core.js clockInfo 同源字段）。 */
interface ClockInfo {
  server_ts: number;
  tz_offset_min: number;
  sign_status?: string;
}
/** GET /api/users 的行。**email（完整邮箱）有值但禁止渲染** —— 展示层一律用
 *  服务端遮罩口径的 display（与 work_users 页同规则，test_web_mask_email_parity 守卫同一纪律）。 */
interface UserRow {
  id: number;
  email: string;
  display: string;
  role: string;
  created_at: string;
  account_count: number;
}

const clock = ref<ClockInfo | null>(null);
const users = ref<UserRow[]>([]);
const loading = ref(false);
const errorText = ref("");
const serverNow = ref("");
const isDark = ref(document.documentElement.getAttribute("data-theme") === "dark");

let clockTimer: ReturnType<typeof setInterval> | null = null;
let clockBaseMs = 0;
let clockFetchedAtMs = 0;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** 服务器墙上时间：epoch + 服务器与 UTC 差 + 时区分量（core.js getServerNow 同口径）。 */
function tickClock(): void {
  if (!clockBaseMs) return;
  const d = new Date(clockBaseMs + (Date.now() - clockFetchedAtMs));
  serverNow.value =
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    ` ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

async function loadAll(): Promise<void> {
  loading.value = true;
  errorText.value = "";
  try {
    const [clockData, usersData] = await Promise.all([
      apiGet<ClockInfo>("/api/clock"),
      apiGet<{ users: UserRow[] }>("/api/users"),
    ]);
    clock.value = clockData;
    clockBaseMs = (clockData.server_ts + clockData.tz_offset_min * 60) * 1000;
    clockFetchedAtMs = Date.now();
    users.value = usersData.users;
    tickClock();
  } catch (err) {
    errorText.value = err instanceof ApiError ? err.message : "网络错误，请重试";
  } finally {
    loading.value = false;
  }
}

function toggleTheme(): void {
  const next = isDark.value ? "light" : "dark";
  if (window.YB?.applyTheme) {
    window.YB.applyTheme(next, true); // 既有机器：写 localStorage + data-theme + 派发 yiban:theme
  } else {
    // dev 壳（无 core.js）：直接写并派发同一事件，让同步路径保持唯一
    document.documentElement.setAttribute("data-theme", next);
    document.dispatchEvent(new CustomEvent("yiban:theme", { detail: { theme: next } }));
  }
}

function onShellTheme(): void {
  isDark.value = document.documentElement.getAttribute("data-theme") === "dark";
}

onMounted(() => {
  void loadAll();
  clockTimer = setInterval(tickClock, 1000);
  document.addEventListener("yiban:theme", onShellTheme);
});

onBeforeUnmount(() => {
  if (clockTimer !== null) clearInterval(clockTimer);
  document.removeEventListener("yiban:theme", onShellTheme);
});

const swatches: Array<{ name: string; varName: string }> = [
  { name: "primary", varName: "--primary" },
  { name: "success", varName: "--success" },
  { name: "warning", varName: "--warning" },
  { name: "danger", varName: "--danger" },
  { name: "info", varName: "--info" },
  { name: "purple", varName: "--purple" },
];

function roleTagType(role: string): "danger" | "primary" | "info" {
  if (role === "admin") return "danger";
  if (role === "user") return "info";
  return "primary";
}
</script>

<template>
  <div class="pilot">
    <section class="panel">
      <header class="panel-head-row">
        <h2 class="panel-title">管线自检</h2>
        <el-switch v-model="isDark" active-text="暗色" inactive-text="浅色" @change="toggleTheme" />
      </header>
      <p class="muted">
        服务器时间（走 GET /api/clock，Vue 响应式走秒，口径同 core.js）：
        <strong class="mono">{{ serverNow || "…" }}</strong>
      </p>
      <p class="muted">
        设计语言延续：EP 组件经 <code class="mono">ep-theme.css</code> 映射 Adminator token——
        下方新旧按钮、色板随外壳深浅色同步。
      </p>
      <div class="token-row">
        <div v-for="s in swatches" :key="s.varName" class="token">
          <span class="token-chip" :style="{ background: `var(${s.varName})` }"></span>
          <span class="mono token-name">{{ s.name }}</span>
        </div>
      </div>
      <div class="btn-row">
        <button type="button" class="btn btn--primary">旧栈 .btn--primary</button>
        <el-button type="primary">EP el-button</el-button>
        <button type="button" class="btn btn--ghost">旧栈 .btn--ghost</button>
        <el-button>EP 默认</el-button>
      </div>
    </section>

    <section class="panel">
      <header class="panel-head-row">
        <h2 class="panel-title">用户列表（GET /api/users）</h2>
        <el-button size="small" :loading="loading" @click="loadAll">重新拉取</el-button>
      </header>
      <el-alert
        v-if="errorText"
        type="error"
        :title="errorText"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
      />
      <el-skeleton v-if="loading && !users.length" :rows="4" animated />
      <el-table v-else :data="users" size="small" empty-text="暂无用户">
        <el-table-column prop="display" label="用户（服务端遮罩口径）" min-width="180" />
        <el-table-column label="角色" width="110">
          <template #default="{ row }">
            <el-tag :type="roleTagType(row.role)" size="small" disable-transitions>{{ row.role }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="account_count" label="账号数" width="90" align="center" />
        <el-table-column prop="created_at" label="注册时间" min-width="160" />
      </el-table>
      <p class="muted small">
        纪律演示：接口返回的完整 email 字段存在于内存态但从不进 DOM —— 展示层只用服务端下发的
        <code class="mono">display</code>（与 <code class="mono">test_web_mask_email_parity</code> 同一口径）。
      </p>
    </section>
  </div>
</template>

<style scoped>
/* 面板骨架只允许消费 Adminator token —— 试点页自身也是「设计语言延续」的验收对象 */
.pilot {
  display: grid;
  gap: 16px;
}
.panel {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 18px;
}
.panel-head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.panel-title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--t-base);
}
.muted {
  margin: 4px 0;
  font-size: 13px;
  color: var(--t-muted);
}
.small {
  font-size: 12px;
}
.mono {
  font-family: var(--font-mono);
  font-size: 12px;
}
.token-row {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin: 12px 0;
}
.token {
  display: flex;
  align-items: center;
  gap: 6px;
}
.token-chip {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  border: 1px solid var(--border);
  display: inline-block;
}
.token-name {
  color: var(--t-muted);
}
.btn-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
</style>
