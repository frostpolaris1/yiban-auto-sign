<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  api,
  confirmDialog,
  dangerousSubmit,
  errorMessage,
  isCanceled,
  maskPhone,
  openChangePassword,
  openConfirmPasswordModal,
  ownerEmailVisible,
  passwordHint,
  passwordPolicyOk,
  setOwnerEmailVisible,
  toast,
} from "../lib/shell";
import { buildAccountPayload, CODE_CLEAR, CODE_CLEAR_BTN, CODE_CLEAR_CANCEL_BTN, CODE_CLEARED_PLACEHOLDER, CODE_PLACEHOLDER, CODE_UNCHANGED_PLACEHOLDER, CREDS_GATE_DESC, PASSWORD_UNCHANGED_PLACEHOLDER, availableUserItems, credsWritten, formSubtitle, formTexts, formTitle, makeSnapshot, submitLabel, type AccountPayload, type SelectItem } from "./accountform";
import { accountActions, accountIcon, accountMeta, auditAriaText, deletedNote, passwordAlertText, passwordAlertVisible, pendingHintText, pendingHintVisible, rejectedAlertText, statusBadge, submitButtonVisible, todayStateText, type MyAccount } from "./model";
import { adminGateDesc, adminSaveFailedTip, adminTip, needsGate, TIP_TTL_MS, userSaveFailedTip, userTip } from "./mailnotify";
import { DISABLED_HINT, LOAD_ERROR_TEXT, SLOT_FULL_PCT, collapseLabel, collapsedByDefault, edgeTip, estimateText, slotClass, slotPctText, slotTitle, type TimePrefData } from "./timepref";
import { browserScheduler, createVerifyJob } from "./useVerifyJob";

/* 「我的账号」（管理端 /my/account）与「账号与设置」（用户端 /user/account）**共用**同一实现
   ——对齐 legacy 的 partials/page_my_accounts.html + components/my-accounts-page.js。
   变体差异（由 data-variant 注入）：
     user  ：可自助注销（危险区 + 注销态卡 + 撤销 + 倒计时登出）；无「竖屏归属邮箱」偏好
     admin ：有归属邮箱偏好；可补今日状态行（前方排队 N 人 / 今日已完成签到）

   纪律：
   · 指标口径全部走 src/myaccounts/*.ts（已单测）；本组件只做装配与请求；
   · 危险操作走 shell 的 confirmDialog / openConfirmPasswordModal（模态栈与门禁在 core.js）；
   · 口令策略与改密弹窗**复用** shell（唯一源在 core.js，跨层元测试盯着）；
   · 完整手机号只在内存（列表项）与请求体，绝不回显进 DOM（管理端编辑）——见 accountform.ts。 */

const props = defineProps<{ variant: "user" | "admin"; calendarHref: string }>();
const isAdmin = computed(() => props.variant === "admin");
const T = computed(() => formTexts(props.variant));

interface Me {
  role?: string;
  email?: string;
  is_builtin_admin?: boolean;
  sign_order?: string;
  sign_window?: string;
  time_pref_allowed?: boolean;
  mail_notify?: boolean;
}

const me = ref<Me | null>(null);
const accounts = ref<MyAccount[]>([]);
const loading = ref(false);
const loadError = ref("");
const flash = ref<Record<number, string>>({});
const rowBusy = ref(false);

const banner = computed(() => {
  const m = me.value;
  if (!m) return "";
  const order = m.sign_order === "random" ? "每天随机安排" : "按固定顺序安排";
  const pref = m.time_pref_allowed ? "可自选签到时间" : "暂不可自选签到时间";
  return `签到方式：${order} · 窗口 ${m.sign_window || ""} · ${pref}`;
});

/* ---------------- 账号列表 ---------------- */
async function loadAccounts(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const data = await api<{ accounts?: MyAccount[] }>("GET", "/api/my-accounts");
    accounts.value = data.accounts ?? [];
  } catch (e) {
    loadError.value = errorMessage(e, "账号列表加载失败，请检查网络后重试");
  } finally {
    loading.value = false;
  }
}

function setFlash(i: number, text: string): void {
  flash.value[i] = text;
  setTimeout(() => {
    delete flash.value[i];
  }, 4000);
}

async function togglePause(i: number): Promise<void> {
  if (rowBusy.value) return;
  const a = accounts.value[i];
  const next = !a.user_paused;
  if (next) {
    const ok = await confirmDialog({
      title: "暂停签到",
      body: `确定暂停「${a.display_name}」的签到吗？暂停后系统将不再自动签到，可随时恢复。`,
      confirmText: "暂停签到",
      danger: true,
    });
    if (!ok) return;
  }
  rowBusy.value = true;
  try {
    const data = await api<{ msg?: string; paused?: boolean }>("PUT", `/api/my-accounts/${i}/pause`, { paused: next });
    a.user_paused = typeof data?.paused === "boolean" ? data.paused : next;
    toast().success(data?.msg || (next ? "已暂停" : "已恢复"));
    setFlash(i, a.user_paused ? "已暂停签到" : "已恢复签到");
  } catch (e) {
    if (!isCanceled(e)) toast().error(errorMessage(e, "操作失败，请稍后重试"));
  } finally {
    rowBusy.value = false;
  }
}

async function deleteAccount(i: number): Promise<void> {
  if (rowBusy.value) return;
  const a = accounts.value[i];
  const ok = await confirmDialog({
    title: "删除账号",
    body: `确定删除「${a.display_name}」(${a.phone}) 吗？删除后 7 天内可撤销恢复，超过 7 天将自动清除。`,
    confirmText: "删除",
    danger: true,
  });
  if (!ok) return;
  rowBusy.value = true;
  try {
    const data = await api<{ msg?: string }>("DELETE", `/api/my-accounts/${i}`);
    toast().success(data?.msg || "已删除，7 天内可撤销");
    a.deleted = true;
    a.deleted_by_me = true;
    setFlash(i, "已删除，7 天内可撤销");
  } catch (e) {
    if (!isCanceled(e)) toast().error(errorMessage(e, "删除失败，请稍后重试"));
  } finally {
    rowBusy.value = false;
  }
}

async function restoreAccount(i: number): Promise<void> {
  if (rowBusy.value) return;
  const a = accounts.value[i];
  const ok = await confirmDialog({
    title: "撤销删除",
    body: `撤销删除「${a.display_name}」(${a.phone})？将恢复到删除前的状态。`,
    confirmText: "撤销删除",
  });
  if (!ok) return;
  rowBusy.value = true;
  try {
    const data = await api<{ msg?: string; accounts?: MyAccount[] }>("POST", `/api/my-accounts/${i}/restore`, {});
    const match = (data.accounts ?? []).find((x) => x.phone === a.phone);
    if (match) Object.assign(a, match);
    else {
      a.deleted = false;
      a.deleted_by_me = false;
    }
    toast().success(data?.msg || "已恢复");
    setFlash(i, "已恢复账号");
  } catch (e) {
    if (!isCanceled(e)) toast().error(errorMessage(e, "恢复失败，请稍后重试"));
  } finally {
    rowBusy.value = false;
  }
}

/* ---------------- 账号表单（用户变体：无邮箱绑定分支） ---------------- */
const formOpen = ref(false);
const formEditing = ref(false);
const formIndex = ref<number | null>(null);
const formError = ref("");
const formBusy = ref(false);
const formDetailLoading = ref(false);
/** 管理端编辑的乐观锁快照（用户端编辑自己的账号无需：不走 detail） */
const formSnapshot = ref<string | null>(null);
const f = ref({ name: "", phone: "", password: "", model: "", code: "" });
const clearCode = ref(false);
const emailItems = ref<SelectItem[]>([]);
const formEmail = ref("");
const formManualEmail = ref("");
const formInitialPassword = ref("");

function openForm(index: number | null): void {
  formEditing.value = index !== null;
  formIndex.value = index;
  formError.value = "";
  formSnapshot.value = null;
  clearCode.value = false;
  formEmail.value = "";
  formManualEmail.value = "";
  formInitialPassword.value = "";
  const a = index !== null ? accounts.value[index] : null;
  f.value = {
    name: a?.display_name ?? "",
    phone: a?.phone ?? "",
    password: "",
    model: a?.phone_model ?? "",
    code: "",
  };
  formOpen.value = true;
  if (isAdmin.value && index !== null) void loadDetail(index);
  if (isAdmin.value && index === null) void loadBindableUsers();
}

/** 管理端编辑：先取 detail（完整号 + 快照）。**改进（legacy 无）**：期间禁用主按钮并提示，
    避免用户在 detail 到达前提交撞上"无法获取账号完整信息"的莫名报错。 */
async function loadDetail(index: number): Promise<void> {
  formDetailLoading.value = true;
  formSnapshot.value = null;
  try {
    const d = await api<{ account?: { phone?: string; name?: string; phone_model?: string; status?: string; deleted?: boolean } }>(
      "GET",
      `/api/accounts/${index}/detail`,
    );
    const acc = d.account ?? {};
    formSnapshot.value = makeSnapshot(acc);
    fullPhone.value = String(acc.phone ?? "");
    if (!accounts.value[index]?.phone) {
      f.value.phone = "";
      fullPhone.value = "";
      formSnapshot.value = null;
    } else {
      f.value.phone = accounts.value[index].phone; // 只回显打码号
    }
  } catch {
    fullPhone.value = "";
    formSnapshot.value = null;
  } finally {
    formDetailLoading.value = false;
  }
}

/** 管理端完整手机号只存此处（内存），提交时取用；绝不回显进输入框 */
const fullPhone = ref("");

async function loadBindableUsers(): Promise<void> {
  try {
    const data = await api<{ users?: Array<{ email?: string; display?: string; account_count?: number }> }>("GET", "/api/users");
    emailItems.value = availableUserItems(data.users ?? []);
  } catch {
    /* 静默：下拉为空仍可手填 */
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
      email: isAdmin.value && !formEditing.value ? formEmail.value : undefined,
      manualEmail: formManualEmail.value,
      initialPassword: formInitialPassword.value,
    },
    {
      editing: formEditing.value,
      allowEmail: isAdmin.value,
      requiresSnapshot: isAdmin.value && formEditing.value,
      snapshot: formSnapshot.value,
      policyOk: passwordPolicyOk,
      policyHint: passwordHint(false),
    },
  );
  if (built.error || !built.payload) {
    formError.value = built.error ?? "表单校验未通过";
    return;
  }
  const payload: AccountPayload = built.payload;
  const endpoint = formEditing.value ? `/api/my-accounts/${formIndex.value}` : "/api/my-accounts";
  const method = formEditing.value ? "PUT" : "POST";
  // 口径 3：只有写密码或改绑手机号才算改写凭据 ⇒ 走受门禁提交（档位只存在于后端）
  const gated = credsWritten(payload, { editing: formEditing.value, snapshot: formSnapshot.value });
  formBusy.value = true;
  try {
    const data = gated
      ? ((await api("POST", endpoint, payload)) as { msg?: string; job_id?: string })
      : ((await api(method, endpoint, payload)) as { msg?: string; job_id?: string });
    formOpen.value = false;
    fullPhone.value = "";
    toast().success(data?.msg || "已保存");
    await loadAccounts();
    if (data?.job_id) verify.start(data.job_id);
    else if (formEditing.value) verify.stop();
  } catch (e) {
    if (isCanceled(e)) return; // 取消门禁口令框不算失败
    formError.value = errorMessage(e, "保存失败，请稍后再试");
  } finally {
    formBusy.value = false;
  }
}

/* ---------------- 在线校验 ---------------- */
const verify = createVerifyJob({
  api: (method, path, body) => api(method, path, body),
  scheduler: browserScheduler,
});
/** 顶层绑定，模板里自动解包（verify.state 是嵌套 ref，读起来要写 .value） */
const verifyState = verify.state;

/* ---------------- 签到时间（自选时段） ---------------- */
const timePref = ref<TimePrefData | null>(null);
const prefExpanded = ref(false);
const prefBusy = ref(false);

async function loadTimePref(preserve = false): Promise<void> {
  try {
    const data = await api<TimePrefData>("GET", "/api/my-time-pref");
    timePref.value = data;
    if (!preserve) prefExpanded.value = !collapsedByDefault(data.allowed);
  } catch {
    timePref.value = null;
  }
}

async function pickSlot(slotMin: number): Promise<void> {
  if (prefBusy.value) return;
  prefBusy.value = true;
  try {
    const data = await api<{ msg?: string }>("PUT", "/api/my-time-pref", { slot_min: slotMin });
    toast().success(data?.msg || "已保存");
    await loadTimePref(true);
  } catch (e) {
    toast().error(errorMessage(e, "保存失败，请稍后重试"));
  } finally {
    prefBusy.value = false;
  }
}

async function clearSlot(): Promise<void> {
  if (prefBusy.value) return;
  prefBusy.value = true;
  try {
    const data = await api<{ msg?: string }>("PUT", "/api/my-time-pref", { slot_min: null });
    toast().success(data?.msg || "已清除");
    await loadTimePref(true);
  } catch (e) {
    toast().error(errorMessage(e, "清除失败，请稍后重试"));
  } finally {
    prefBusy.value = false;
  }
}

/* ---------------- 邮件提醒（双变体：普通用户 / 内置主管理员走 mail-config 且关门禁） ---------------- */
const mailOn = ref(false);
const mailBusy = ref(false);
const mailTip = ref("");
let mailTipTimer: ReturnType<typeof setTimeout> | null = null;

function setMailTip(text: string, ttl = false): void {
  mailTip.value = text;
  if (mailTipTimer) clearTimeout(mailTipTimer);
  if (ttl) mailTipTimer = setTimeout(() => (mailTip.value = ""), TIP_TTL_MS);
}

async function initMail(): Promise<void> {
  if (me.value?.is_builtin_admin) {
    try {
      const data = await api<{ admin_notify?: boolean }>("GET", "/api/mail-config");
      mailOn.value = !!data.admin_notify;
    } catch {
      /* 读取失败保持开关不动，写入时后端仍会判定 */
    }
  } else {
    mailOn.value = !!me.value?.mail_notify;
  }
}

async function toggleMail(): Promise<void> {
  if (mailBusy.value) return;
  const want = mailOn.value;
  if (me.value?.is_builtin_admin) {
    // 关闭方向受门禁：先回滚 UI，由后端 reason 决定是否补口令；取消弹窗保持原样
    if (!needsGate(want)) {
      mailOn.value = true;
      return;
    }
    mailOn.value = true;
    mailBusy.value = true;
    setMailTip("保存中…");
    try {
      await dangerousSubmit({ method: "PUT", path: "/api/mail-config", body: { admin_notify: false }, desc: adminGateDesc() });
      mailOn.value = false;
      setMailTip(adminTip(false), true);
    } catch (e) {
      if (isCanceled(e)) setMailTip("");
      else setMailTip(adminSaveFailedTip());
    } finally {
      mailBusy.value = false;
    }
    return;
  }
  mailBusy.value = true;
  setMailTip("保存中…");
  try {
    await api("PUT", "/api/my-mail-notify", { enabled: want });
    setMailTip(userTip(want), true);
  } catch (e) {
    mailOn.value = !want; // 失败回滚
    setMailTip(userSaveFailedTip(errorMessage(e, "")));
  } finally {
    mailBusy.value = false;
  }
}

/* ---------------- 显示偏好（仅管理端） ---------------- */
const ownerEmailOn = ref(false);
const ownerEmailTip = ref("");
function toggleOwnerEmail(): void {
  setOwnerEmailVisible(ownerEmailOn.value);
  ownerEmailTip.value = ownerEmailOn.value ? "已开启（账号页下次渲染即生效）" : "已关闭";
}

/* ---------------- 注销（仅用户端；软删除 + 7 天宽限 + 撤销） ---------------- */
const deletedMode = ref(false);
const logoutRemain = ref(0);
let logoutTimer: ReturnType<typeof setInterval> | null = null;

function scheduleLogout(): void {
  const total = 8;
  logoutRemain.value = total;
  logoutTimer = setInterval(() => {
    logoutRemain.value -= 1;
    if (logoutRemain.value <= 0) finishLogout();
  }, 1000);
}
function finishLogout(): void {
  if (logoutTimer) clearInterval(logoutTimer);
  logoutTimer = null;
  try {
    localStorage.clear();
  } catch {
    /* 受限环境忽略 */
  }
  location.href = "/login";
}

async function deleteSelf(): Promise<void> {
  const ok = await confirmDialog({
    title: "注销账号",
    body: "注销将删除你的账号、易班账号与自选签到时间。7 天内可撤销恢复，超过 7 天将永久删除，无法找回。",
    confirmText: "继续注销",
    danger: true,
  });
  if (!ok) return;
  openConfirmPasswordModal(
    "注销后账号将无法登录，易班账号与自选签到时间会被删除；7 天宽限期内可撤销。请输入当前密码完成注销。",
    (pw) => {
      void api("POST", "/api/me/delete", { password: pw }).then(
        (data) => {
          toast().success((data as { msg?: string })?.msg || "账号已注销");
          deletedMode.value = true;
          scheduleLogout();
        },
        (e) => toast().error(errorMessage(e, "注销失败，请稍后再试")),
      );
    },
  );
}

function restoreSelf(): void {
  const email = me.value?.email ?? "";
  if (!email) return;
  openConfirmPasswordModal(
    "撤销注销将恢复你的账号、易班账号与自选签到时间。请输入当前密码确认。",
    (pw) => {
      void api("POST", "/api/me/restore", { email, password: pw }).then(
        () => {
          if (logoutTimer) clearInterval(logoutTimer);
          logoutTimer = null;
          toast().success("已撤销注销，账号已恢复");
          location.reload();
        },
        (e) => toast().error(errorMessage(e, "撤销失败，请稍后再试")),
      );
    },
  );
}

/** 侧栏账号区：外壳的 [data-account-email] 只显示**邮箱本地部**（legacy renderSidebarEmail
    口径：本地部上屏、完整邮箱进 title）。不补这一步，外壳会显示完整邮箱——与迁移前不一致。 */
function renderSidebarEmail(m: Me): void {
  const email = String(m.email || "");
  if (!email) return;
  document.querySelectorAll<HTMLElement>("[data-account-email]").forEach((node) => {
    node.textContent = email.split("@")[0];
    node.title = email;
  });
}

/* ---------------- 装配 ---------------- */
onMounted(async () => {
  try {
    const identity = await api<Me>("GET", "/api/me");
    me.value = identity;
    const expected = isAdmin.value ? "admin" : "user";
    if (identity.role !== expected) {
      location.href = isAdmin.value ? "/user/account" : "/data/dashboard";
      return;
    }
    renderSidebarEmail(identity);
    ownerEmailOn.value = ownerEmailVisible();
    await Promise.all([loadAccounts(), loadTimePref(), initMail()]);
  } catch {
    location.href = "/login";
  }
});
</script>

<template>
  <div class="myaccounts">
    <div class="page-head">
      <h1 class="page-title">{{ isAdmin ? "我的账号" : "账号与设置" }}</h1>
      <p class="page-sub">
        {{ isAdmin ? "管理本人的易班账号、自选签到时间与登录凭据。" : "管理你的易班账号、自选签到时间与登录凭据。" }}
      </p>
    </div>

    <p v-if="banner" class="alert info schedule-banner" role="status">
      <span class="ico"><svg aria-hidden="true"><use href="#i-info" /></svg></span>
      <span class="body">{{ banner }}</span>
    </p>

    <div class="user-grid">
      <!-- 已注销态（仅用户端）：就地渲染终态卡 + 撤销入口 + 登出倒计时 -->
      <section v-if="deletedMode" class="card col-12">
        <div class="panel-head panel-head--center">
          <div class="panel-head-text">
            <h2 class="panel-title">账号已注销</h2>
            <p class="panel-sub">账号、易班账号与自选签到时间已删除；7 天内可撤销恢复，超期将永久删除。</p>
          </div>
          <button v-if="me?.email" type="button" class="btn btn--primary btn--sm" @click="restoreSelf">撤销注销</button>
        </div>
        <p class="panel-sub" role="status" aria-live="polite">
          {{ me?.email ? `将在 ${logoutRemain} 秒后退出登录；可先点「撤销注销」恢复账号。` : `将在 ${logoutRemain} 秒后退出登录。` }}
        </p>
        <a v-if="!me?.email" class="btn btn--primary btn--sm" href="/login">去登录页恢复账号</a>
      </section>

      <!-- 我的账号 -->
      <section v-if="!deletedMode" class="card col-12">
        <div class="panel-head panel-head--center">
          <div class="panel-head-text">
            <h2 class="panel-title">我的账号</h2>
            <p class="panel-sub">审核通过后自动签到。</p>
          </div>
          <button v-if="submitButtonVisible(accounts)" type="button" class="btn btn--primary" @click="openForm(null)">
            <svg aria-hidden="true"><use href="#i-plus" /></svg>提交我的易班账号
          </button>
        </div>

        <p v-if="verifyState.visible" class="set-tip" :class="{ 'set-warn': verifyState.bad }" aria-live="polite">
          <span>{{ verifyState.text }}</span>
          <button
            v-if="verifyState.cancellable"
            type="button"
            class="btn btn--ghost btn--sm"
            @click="verify.cancel()"
          >取消校验</button>
        </p>

        <p v-if="loadError" class="alert danger" role="alert">
          <span class="ico"><svg aria-hidden="true"><use href="#i-circle-alert" /></svg></span>
          <span class="body">{{ loadError }}</span>
          <button type="button" class="btn btn--ghost btn--sm" @click="loadAccounts">重试</button>
        </p>
        <p v-else-if="!accounts.length && !loading" class="empty">
          <span class="empty__icon"><svg aria-hidden="true"><use href="#i-user" /></svg></span>
          <span class="empty__msg">还没有提交账号。审核通过后自动签到。</span>
        </p>

        <div class="account-list">
          <div v-for="(a, i) in accounts" :key="`${a.phone}-${i}`" class="account-card">
            <div class="account-head">
              <div class="account-ident">
                <span class="account-icon"><svg aria-hidden="true"><use :href="`#i-${accountIcon(a)}`" /></svg></span>
                <span class="sr-only">{{ auditAriaText(a) }}</span>
                <div>
                  <div class="account-title-row">
                    <span class="account-name">{{ a.display_name }}</span>
                    <span class="badge" :class="statusBadge(a).tone ? `badge--${statusBadge(a).tone}` : ''">{{ statusBadge(a).text }}</span>
                  </div>
                  <div class="account-meta">{{ accountMeta(a) }}</div>
                  <div v-if="isAdmin && todayStateText(a)" class="account-note">{{ todayStateText(a) }}</div>
                  <div v-if="a.deleted" class="account-note">{{ deletedNote(a) }}</div>
                </div>
              </div>
              <div class="account-actions">
                <template v-for="act in accountActions(a, { calendarHref: props.calendarHref })" :key="act.kind">
                  <a v-if="act.kind === 'calendar'" class="btn btn--ghost btn--sm" :href="props.calendarHref">签到日历</a>
                  <button v-else-if="act.kind === 'pause'" type="button" class="btn btn--ghost btn--sm" :disabled="rowBusy" @click="togglePause(i)">
                    {{ act.label }}
                  </button>
                  <button v-else-if="act.kind === 'edit'" type="button" class="btn btn--ghost btn--sm" @click="openForm(i)">{{ act.label }}</button>
                  <button v-else-if="act.kind === 'delete'" type="button" class="btn btn--ghost btn--danger-ghost btn--sm" :disabled="rowBusy" @click="deleteAccount(i)">
                    删除
                  </button>
                  <button v-else-if="act.kind === 'restore'" type="button" class="btn btn--ghost btn--sm" :disabled="rowBusy" @click="restoreAccount(i)">
                    {{ act.label }}
                  </button>
                  <span v-else class="account-note">{{ "text" in act ? act.text : "" }}</span>
                </template>
              </div>
            </div>

            <p v-if="flash[i]" class="state-line state-line--ok state-line--flash" role="status">{{ flash[i] }}</p>

            <div v-if="rejectedAlertText(a)" class="alert danger account-reject" role="status">
              <span class="ico"><svg aria-hidden="true"><use href="#i-circle-alert" /></svg></span>
              <span class="body">{{ rejectedAlertText(a) }}</span>
            </div>
            <div v-if="passwordAlertVisible(a)" class="alert danger account-reject" role="status">
              <span class="ico"><svg aria-hidden="true"><use href="#i-circle-alert" /></svg></span>
              <span class="body">{{ passwordAlertText() }}</span>
            </div>

            <details v-if="a.logs && a.logs.length" class="account-details">
              <summary>最近签到记录（{{ a.logs.length }} 条）</summary>
              <pre class="log-view">{{ a.logs.join("\n") }}</pre>
            </details>
            <p v-if="pendingHintVisible(a)" class="account-pending-hint">{{ pendingHintText() }}</p>
          </div>
        </div>
      </section>

      <!-- 签到时间（自选时段；未开启时默认收起） -->
      <section v-if="!deletedMode && timePref && timePref.has_account" class="card col-12">
        <div class="panel-head panel-head--center">
          <div class="panel-head-text">
            <h2 class="panel-title">签到时间</h2>
            <p class="panel-sub">窗口 <span class="mono">{{ timePref.window }}</span> · 5 分钟一段</p>
          </div>
          <button
            type="button"
            class="btn btn--ghost btn--sm"
            :aria-expanded="prefExpanded ? 'true' : 'false'"
            @click="prefExpanded = !prefExpanded"
          >
            <span>{{ collapseLabel(!prefExpanded) }}</span>
            <span class="collapse-arrow"><svg aria-hidden="true"><use href="#i-chevron-down" /></svg></span>
          </button>
        </div>
        <div class="collapse-body" :class="{ 'is-open': prefExpanded }">
          <div class="collapse-inner">
            <p v-if="!estimateText(timePref).hidden" class="panel-sub pref-estimate">{{ estimateText(timePref).text }}</p>
            <div class="slot-grid" role="group" aria-label="自选签到时段">
              <button
                v-for="(s, i) in timePref.slots"
                :key="s.slot_min"
                type="button"
                class="slot"
                :class="slotClass(s, timePref.pref_slot)"
                :disabled="!!s.disabled || prefBusy"
                :title="slotTitle(s)"
                @click="pickSlot(s.slot_min)"
              >
                <div class="slot-name">{{ s.label }}</div>
                <div class="slot-pct">{{ slotPctText(s) }}</div>
                <div v-if="edgeTip(timePref, i, timePref.slots.length)" class="sr-only">{{ edgeTip(timePref, i, timePref.slots.length) }}</div>
              </button>
            </div>
            <div class="form-actions pref-foot">
              <button type="button" class="btn btn--ghost btn--sm" :disabled="prefBusy" @click="clearSlot">清除选择（恢复自动分配）</button>
              <span class="panel-sub" aria-live="polite">{{ edgeTip(timePref, timePref.slots.findIndex((s) => s.slot_min === timePref.pref_slot), timePref.slots.length) }}</span>
            </div>
            <p v-if="!timePref.allowed" class="alert warning pref-hint">
              <span class="ico"><svg aria-hidden="true"><use href="#i-triangle-alert" /></svg></span>
              <span class="body">{{ DISABLED_HINT }}</span>
            </p>
            <p v-if="!timePref" class="panel-sub">{{ LOAD_ERROR_TEXT }}</p>
          </div>
        </div>
      </section>

      <!-- 邮件提醒 -->
      <section v-if="!deletedMode" class="card col-6 panel-stack">
        <div class="panel-head panel-head--center">
          <div class="panel-head-text">
            <h2 class="panel-title">邮件提醒</h2>
            <p class="panel-sub">仅签到失败时向注册邮箱发送提醒</p>
          </div>
          <label class="switch" title="接收签到失败与告警邮件">
            <input v-model="mailOn" type="checkbox" :disabled="mailBusy" @change="toggleMail" />
            <span class="track" aria-hidden="true"></span>
            <span class="sr-only">接收签到失败与告警邮件</span>
          </label>
        </div>
        <p class="panel-sub" aria-live="polite">{{ mailTip }}</p>
      </section>

      <!-- 修改密码（复用 core.js 的共享弹窗：策略判定与改密后跳登录都在那里） -->
      <section v-if="!deletedMode" class="card col-6 panel-stack">
        <div class="panel-head panel-head--center">
          <div class="panel-head-text">
            <h2 class="panel-title">修改密码</h2>
            <p class="panel-sub">改后下次登录使用新密码，当前会话随即失效</p>
          </div>
          <button type="button" class="btn btn--ghost btn--sm" @click="openChangePassword({ builtinAdmin: !!me?.is_builtin_admin })">
            <svg aria-hidden="true"><use href="#i-key" /></svg>修改密码
          </button>
        </div>
      </section>

      <!-- 显示偏好（仅管理端） -->
      <section v-if="isAdmin && !deletedMode" class="card col-12 panel-stack">
        <div class="panel-head">
          <div class="panel-head-row">
            <h2 class="panel-title">显示偏好</h2>
          </div>
          <p class="panel-sub">仅影响本机显示密度，不写入服务器配置。</p>
        </div>
        <div class="set-row">
          <div class="set-row-text">
            <span class="set-row-label">竖屏显示归属邮箱</span>
            <p class="set-help">窄屏在名称下补一行归属邮箱（已脱敏）；宽屏归属列不受影响。</p>
          </div>
          <label class="switch" title="竖屏显示归属邮箱">
            <input v-model="ownerEmailOn" type="checkbox" @change="toggleOwnerEmail" />
            <span class="track" aria-hidden="true"></span>
            <span class="sr-only">竖屏显示归属邮箱</span>
          </label>
        </div>
        <p class="set-tip" role="status">{{ ownerEmailTip }}</p>
      </section>

      <!-- 注销账号（仅用户端） -->
      <section v-if="!isAdmin && !deletedMode" class="card col-12 user-danger">
        <div class="panel-head panel-head--center">
          <div class="panel-head-text">
            <h2 class="panel-title">注销账号</h2>
            <p class="panel-sub">账号与自选时间一并删除，7 天内可撤销</p>
          </div>
          <button type="button" class="btn btn--ghost btn--danger-ghost" @click="deleteSelf">
            <svg aria-hidden="true"><use href="#i-trash" /></svg>注销账号
          </button>
        </div>
      </section>
    </div>

    <!-- 新增/编辑账号弹窗 -->
    <el-dialog
      v-model="formOpen"
      :title="formTitle({ editing: formEditing, variant: props.variant, index: formIndex })"
      width="560px"
      append-to-body
    >
      <p class="panel-sub" style="margin-top: 0">{{ formSubtitle({ editing: formEditing, variant: props.variant }) }}</p>
      <el-alert v-if="formError" type="error" :title="formError" :closable="false" show-icon style="margin-bottom: 10px" />

      <div class="form-stack">
        <label class="field">
          <span class="field-label">{{ T.nameLabel }}</span>
          <input v-model="f.name" class="input" type="text" maxlength="50" :placeholder="T.namePlaceholder" />
          <span class="field-help">{{ T.nameHelp }}</span>
        </label>

        <label v-if="isAdmin && !formEditing" class="field">
          <span class="field-label">绑定用户（选填）</span>
          <el-select v-model="formEmail" style="width: 100%">
            <el-option v-for="(it, idx) in emailItems" :key="idx" :value="it.v ?? ''" :label="it.t ?? it.group ?? it.empty ?? ''" />
          </el-select>
        </label>

        <label class="field">
          <span class="field-label">{{ T.phoneLabel }}<span class="req" aria-hidden="true">*</span></span>
          <input v-model="f.phone" class="input" type="tel" inputmode="numeric" maxlength="20" :placeholder="T.phonePlaceholder" />
          <span v-if="formEditing && isAdmin" class="field-help">{{ T.phoneEditHelp }}</span>
          <span v-if="formDetailLoading" class="field-help">正在获取账号完整信息…</span>
        </label>

        <label class="field">
          <span class="field-label">{{ T.passwordLabel }}<span v-if="!formEditing" class="req" aria-hidden="true">*</span></span>
          <input
            v-model="f.password"
            class="input"
            type="password"
            autocomplete="new-password"
            :placeholder="formEditing ? PASSWORD_UNCHANGED_PLACEHOLDER : T.passwordNewPlaceholder"
          />
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
            :placeholder="clearCode ? CODE_CLEARED_PLACEHOLDER : formEditing ? CODE_UNCHANGED_PLACEHOLDER : CODE_PLACEHOLDER"
          />
          <span class="field-help">{{ T.codeHelp }}</span>
        </label>
      </div>
      <template #footer>
        <button type="button" class="btn btn--ghost" @click="formOpen = false">取消</button>
        <button type="button" class="btn btn--primary" :disabled="formBusy || formDetailLoading" @click="submitForm">
          {{ submitLabel({ editing: formEditing, variant: props.variant }) }}
        </button>
      </template>
    </el-dialog>
  </div>
</template>
