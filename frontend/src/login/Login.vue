<script setup lang="ts">
import { computed, nextTick, onMounted, ref, type Ref } from "vue";
import {
  api,
  openModal,
  passwordClasses,
  passwordHint,
  pwMinClasses,
  pwMinLen,
  shellBase,
} from "../lib/shell";
import {
  AUTH_TITLES,
  canSwitchTo,
  checkConfirm,
  checkEmail,
  checkPassword,
  landingPath,
  loginOutcome,
  networkErrorText,
  registerGate,
  restoreOutcome,
  type AuthMode,
  type FieldCheck,
  type LoginResponse,
  type PasswordPolicy,
} from "./model";

/* 登录 / 注册页（`/login`，唯一在登录态之外渲染的页面）。

   与 legacy `web/static/js/pages/login.js`（已退役）逐项对应：
   页签切换（含淡入与 aria-selected）、注册暂停探测、三个密码可见性开关、
   注册字段级即时校验、登录提交（含注销冷静期 recoverable 分支）、冷静期恢复、
   注册提交（注册成功即自动登录）、协议/隐私模态、更新日志模态（由外壳承担）。

   ## 三处刻意的边界
   1. **口令策略不自带**：`PW_MIN_LEN/PW_MIN_CLASSES/PW_CLASS_PATTERNS` 的唯一实现是
      `core.js`（与后端跨层对拍，见 tests/test_rekey_key_source.py），这里经
      `lib/shell.ts` 桥接成 `PasswordPolicy` 注入口径层——本页只决定"拿策略做什么判断"。
   2. **协议正文不从组件渲染**：正文是服务端渲染好的 HTML（`_read_doc_html`），放在页面
      的惰性 `<template id="doc-*">` 里，本组件只把它 cloneNode 进 core.js 的模态。
      故本页零 `v-html`——把服务端 HTML 塞进 Vue 模板才需要那种逃生口。
   3. **契约 id 一律沿用 legacy**（`#username`/`#password`/`#login-btn`/`#error-box`/`#reg-*`）：
      e2e 的真表单登录与错误路径断言按它们取值，改名等于让守卫静默失守。

   ## 与 legacy 的一处有意取舍
   legacy 的表单是**服务端渲染**的（JS 挂了也能看见字段，但提交本就依赖 JS 的 fetch，
   原生 POST 会撞 405）。迁到 Vue 后整页只剩挂载点，无 JS 时由模板里的 `<noscript>` 提示顶上。 */

const policy: PasswordPolicy = {
  minLen: pwMinLen(),
  minClasses: pwMinClasses(),
  classes: passwordClasses,
  hint: passwordHint(false),
};

/* ---------------- 页签 ---------------- */
const mode = ref<AuthMode>("login");
const paused = ref(false);
const loginForm = ref<HTMLFormElement | null>(null);
const registerForm = ref<HTMLFormElement | null>(null);

/** 切页签：整组显隐（表单 + 该模式的切换提示），标题同步，重播进入动画。 */
async function switchMode(m: AuthMode): Promise<void> {
  if (!canSwitchTo(m, paused.value)) return;
  mode.value = m;
  await nextTick();
  const form = m === "login" ? loginForm.value : registerForm.value;
  if (form) {
    // 移除类 → 强制回流 → 加回：重触发 CSS 动画（legacy 同法；只靠 hidden 切换重播
    // 依赖"display 变化会重启动画"的隐式行为，不如显式重播可靠）
    form.classList.remove("tab-enter");
    void form.offsetWidth;
    form.classList.add("tab-enter");
  }
}

/* ---------------- 注册暂停 ---------------- */
// 仅按公开布尔渲染 UI；真正的拦截在后端 /api/register（403），此处被绕过也无效。
async function probeRegistrationPause(): Promise<void> {
  try {
    const data = await api<{ paused?: boolean }>("GET", "/api/registration_paused");
    paused.value = !!(data && data.paused);
  } catch {
    /* 状态获取失败不阻断登录页；后端注册 API 仍会拦截 */
  }
}

/* ---------------- 错误框 ---------------- */
const loginError = ref("");
const regError = ref("");
const loginErrorBox = ref<HTMLElement | null>(null);
const regErrorBox = ref<HTMLElement | null>(null);

/** 显示顶部错误框并聚焦它（tabindex="-1"）——读屏因此能即时播报。 */
async function showError(
  box: Ref<HTMLElement | null>,
  target: Ref<string>,
  msg: string,
): Promise<void> {
  target.value = msg;
  await nextTick();
  box.value?.focus();
}
function hideError(target: Ref<string>): void {
  target.value = "";
}

/* ---------------- 登录表单字段 ---------------- */
const loginEmail = ref("");
const loginPassword = ref("");

/* ---------------- 字段级即时校验 ---------------- */
const regEmail = ref("");
const regPw = ref("");
const regPw2 = ref("");
const agree = ref(false);
const emailCheck = ref<FieldCheck>({ invalid: false, message: "" });
const pwCheck = ref<FieldCheck>({ invalid: false, message: "" });
const pw2Check = ref<FieldCheck>({ invalid: false, message: "" });

function onEmailInput(): void {
  emailCheck.value = checkEmail(regEmail.value);
}
function onPwInput(): void {
  pwCheck.value = checkPassword(regPw.value, policy);
  pw2Check.value = checkConfirm(regPw.value, regPw2.value);
}
function onPw2Input(): void {
  pw2Check.value = checkConfirm(regPw.value, regPw2.value);
}

/* ---------------- 密码可见性 ---------------- */
const showPw = ref(false);
const showRegPw = ref(false);
const showRegPw2 = ref(false);

/* ---------------- 提交 ---------------- */
const loginBusy = ref(false);
const regBusy = ref(false);
const restoreBusy = ref(false);
/** 冷静期恢复入口：仅当登录响应携带 recoverable 时显形 */
const showRestore = ref(false);

async function submitLogin(): Promise<void> {
  if (loginBusy.value) return;
  hideError(loginError);
  showRestore.value = false;
  loginBusy.value = true;
  try {
    const data = await api<LoginResponse>("POST", "/api/login", {
      username: loginEmail.value,
      password: loginPassword.value,
    });
    const out = loginOutcome(data);
    if (out.kind === "ok") {
      location.href = shellBase() + out.path;
      return;
    }
    if (out.kind === "recoverable") {
      await showError(loginErrorBox, loginError, out.message);
      showRestore.value = true;
      return;
    }
    await showError(loginErrorBox, loginError, out.message);
  } catch (e) {
    await showError(loginErrorBox, loginError, networkErrorText(e));
  } finally {
    loginBusy.value = false;
  }
}

async function submitRestore(): Promise<void> {
  if (restoreBusy.value) return;
  restoreBusy.value = true;
  try {
    const data = await api<LoginResponse>("POST", "/api/me/restore", {
      email: loginEmail.value.trim(),
      password: loginPassword.value,
    });
    const out = restoreOutcome(data);
    if (out.kind === "ok") {
      location.href = shellBase() + out.path;
      return;
    }
    await showError(loginErrorBox, loginError, out.message);
    showRestore.value = false; // 恢复失败：收起入口（凭据不对，再点也是同一个结果）
  } catch (e) {
    await showError(loginErrorBox, loginError, networkErrorText(e));
  } finally {
    restoreBusy.value = false;
  }
}

async function submitRegister(): Promise<void> {
  if (regBusy.value) return;
  hideError(regError);
  const gate = registerGate({
    email: regEmail.value,
    password: regPw.value,
    confirm: regPw2.value,
    agree: agree.value,
    policy,
  });
  emailCheck.value = gate.email;
  pwCheck.value = gate.password;
  pw2Check.value = gate.confirm;
  if (gate.blocked) {
    if (gate.topMessage) await showError(regErrorBox, regError, gate.topMessage);
    return;
  }
  regBusy.value = true;
  const email = regEmail.value.trim();
  const password = regPw.value;
  try {
    await api("POST", "/api/register", { email, password, agree: true });
    const login = await api<LoginResponse>("POST", "/api/login", {
      username: email,
      password,
    });
    if (login && login.ok) {
      location.href = shellBase() + landingPath(login.role); // 注册即登录成功：落在日历页
      return;
    }
    await showError(regErrorBox, regError, "注册成功，但自动登录失败，请手动登录");
  } catch (e) {
    await showError(regErrorBox, regError, networkErrorText(e));
  } finally {
    regBusy.value = false;
  }
}

/* ---------------- 协议 / 隐私模态 ---------------- */
// 正文取自服务端渲染的惰性 <template>，克隆后交给 core.js 的模态管理器
// （Esc、Tab 焦点圈定、滚动锁、焦点归还都在那里）。
function openDoc(kind: "agreement" | "privacy"): void {
  const tpl = document.getElementById("doc-" + kind) as HTMLTemplateElement | null;
  if (!tpl) return;
  const body = document.createElement("div");
  body.className = "md-body"; // 文档排印类（见 app.css 第 17 节）
  body.appendChild(tpl.content.cloneNode(true));
  openModal({
    title: kind === "agreement" ? "用户协议" : "隐私政策",
    size: "lg",
    body,
    actions: [{ label: "我知道了", variant: "primary" }],
  });
}

const title = computed(() => AUTH_TITLES[mode.value][0]);
const sub = computed(() => AUTH_TITLES[mode.value][1]);

onMounted(() => {
  void probeRegistrationPause();
});
</script>

<template>
  <div class="auth-page">
    <h2 id="auth-title">{{ title }}</h2>
    <p id="auth-sub" class="sub">{{ sub }}</p>

    <div class="tabs pills auth-tabs auth-seg" role="tablist" aria-label="登录 / 注册切换">
      <button
        id="tab-login"
        type="button"
        class="tab"
        :class="{ 'is-active': mode === 'login' }"
        role="tab"
        :aria-selected="mode === 'login' ? 'true' : 'false'"
        aria-controls="login-form"
        data-auth-tab="login"
        @click="switchMode('login')"
      >
        登录
      </button>
      <button
        id="tab-register"
        type="button"
        class="tab"
        :class="{ 'is-active': mode === 'register' }"
        role="tab"
        :aria-selected="mode === 'register' ? 'true' : 'false'"
        aria-controls="register-form"
        data-auth-tab="register"
        :disabled="paused"
        :title="paused ? '注册已暂停' : ''"
        @click="switchMode('register')"
      >
        注册
      </button>
    </div>

    <!-- 注册暂停提示：管理端暂停注册时显形并禁用注册 tab。
         仅按公开布尔渲染 UI，真正拦截在后端 /api/register（403）。 -->
    <p id="register-paused-hint" class="alert warning" role="status" :hidden="!paused">
      <span class="ico"><svg aria-hidden="true"><use href="#i-triangle-alert" /></svg></span>
      <span class="body">注册已暂停，请联系管理员添加账号。已注册用户可直接登录。</span>
    </p>

    <div id="login-pane" class="auth-pane" :hidden="mode !== 'login'">
      <!-- 登录表单 -->
      <form ref="loginForm" id="login-form" class="auth-form tab-enter" novalidate @submit.prevent="submitLogin">
        <div class="field">
          <label class="field-label" for="username">登录邮箱</label>
          <input
            id="username"
            v-model="loginEmail"
            name="username"
            type="text"
            class="input"
            required
            autocomplete="username"
            inputmode="email"
            placeholder="注册时填写的邮箱"
          />
        </div>
        <div class="field">
          <label class="field-label" for="password">密码</label>
          <div class="input-affix">
            <input
              id="password"
              v-model="loginPassword"
              name="password"
              :type="showPw ? 'text' : 'password'"
              class="input"
              required
              autocomplete="current-password"
              placeholder="输入密码"
            />
            <button
              type="button"
              class="affix-btn"
              data-pw-toggle="password"
              :aria-label="showPw ? '隐藏密码' : '显示密码'"
              :aria-pressed="showPw ? 'true' : 'false'"
              title="显示/隐藏密码"
              @click="showPw = !showPw"
            >
              <svg aria-hidden="true"><use :href="showPw ? '#i-eye-off' : '#i-eye'" /></svg>
            </button>
          </div>
        </div>
        <!-- 提交失败提示：整块由 hidden 控制显隐；文案走插值（防 XSS），
             tabindex="-1" 让 showError 能聚焦并触发读屏播报。 -->
        <div
          id="error-box"
          ref="loginErrorBox"
          class="alert danger"
          role="alert"
          tabindex="-1"
          :hidden="!loginError"
        >
          <span class="ico"><svg aria-hidden="true"><use href="#i-circle-alert" /></svg></span>
          <span class="body" id="login-error-text">{{ loginError }}</span>
        </div>
        <!-- 冷静期恢复入口：仅当登录响应携带 recoverable 时显形（账号已注销且 7 天内） -->
        <button
          id="restore-btn"
          type="button"
          class="btn btn--warning btn--block"
          :hidden="!showRestore"
          :disabled="restoreBusy"
          @click="submitRestore"
        >
          恢复我的账号（7 天内可撤销）
        </button>
        <button id="login-btn" type="submit" class="btn btn--primary btn--block auth-submit" :disabled="loginBusy">
          <span id="login-btn-text">登录</span>
        </button>
      </form>
      <!-- 切换提示放在表单之外：它是「去哪」的导航提示，不是字段帮助；放在字段之间会
           切断字段分组。提示与表单同处一个 .auth-pane：切页签时整组显隐，
           避免出现「表单已隐藏、提示还在」的双提示。 -->
      <p class="auth-switch-hint">
        还没有账号？
        <button
          type="button"
          data-auth-switch="register"
          :disabled="paused"
          :title="paused ? '注册已暂停' : ''"
          @click="switchMode('register')"
        >
          创建账号
        </button>
      </p>
    </div>

    <div id="register-pane" class="auth-pane" :hidden="mode !== 'register'">
      <!-- 注册表单 -->
      <form ref="registerForm" id="register-form" class="auth-form" novalidate @submit.prevent="submitRegister">
        <div class="field">
          <label class="field-label" for="reg-email">邮箱 <span class="req" aria-hidden="true">*</span></label>
          <input
            id="reg-email"
            v-model="regEmail"
            type="email"
            class="input"
            :class="{ 'is-invalid': emailCheck.invalid }"
            required
            autocomplete="email"
            maxlength="64"
            aria-describedby="reg-email-error"
            placeholder="输入邮箱地址"
            @input="onEmailInput"
          />
          <p id="reg-email-error" class="field-error" :hidden="!emailCheck.invalid">{{ emailCheck.message }}</p>
          <p class="field-help">请使用常用邮箱注册，尤其注意 QQ 邮箱格式。</p>
        </div>
        <div class="field">
          <label class="field-label" for="reg-password">密码 <span class="req" aria-hidden="true">*</span></label>
          <div class="input-affix">
            <input
              id="reg-password"
              v-model="regPw"
              :type="showRegPw ? 'text' : 'password'"
              class="input"
              :class="{ 'is-invalid': pwCheck.invalid }"
              required
              autocomplete="new-password"
              aria-describedby="reg-password-error"
              placeholder="设置密码（至少 10 位）"
              @input="onPwInput"
            />
            <button
              type="button"
              class="affix-btn"
              data-pw-toggle="reg-password"
              :aria-label="showRegPw ? '隐藏密码' : '显示密码'"
              :aria-pressed="showRegPw ? 'true' : 'false'"
              title="显示/隐藏密码"
              @click="showRegPw = !showRegPw"
            >
              <svg aria-hidden="true"><use :href="showRegPw ? '#i-eye-off' : '#i-eye'" /></svg>
            </button>
          </div>
          <p id="reg-password-error" class="field-error" :hidden="!pwCheck.invalid">{{ pwCheck.message }}</p>
          <p class="field-help">至少 10 位，且包含大小写字母、数字、符号中的至少两类</p>
        </div>
        <div class="field">
          <label class="field-label" for="reg-password2">确认密码 <span class="req" aria-hidden="true">*</span></label>
          <div class="input-affix">
            <input
              id="reg-password2"
              v-model="regPw2"
              :type="showRegPw2 ? 'text' : 'password'"
              class="input"
              :class="{ 'is-invalid': pw2Check.invalid }"
              required
              autocomplete="new-password"
              aria-describedby="reg-password2-error"
              placeholder="再次输入密码"
              @input="onPw2Input"
            />
            <button
              type="button"
              class="affix-btn"
              data-pw-toggle="reg-password2"
              :aria-label="showRegPw2 ? '隐藏密码' : '显示密码'"
              :aria-pressed="showRegPw2 ? 'true' : 'false'"
              title="显示/隐藏密码"
              @click="showRegPw2 = !showRegPw2"
            >
              <svg aria-hidden="true"><use :href="showRegPw2 ? '#i-eye-off' : '#i-eye'" /></svg>
            </button>
          </div>
          <p id="reg-password2-error" class="field-error" :hidden="!pw2Check.invalid">{{ pw2Check.message }}</p>
        </div>
        <!-- 协议勾选：说明文字里的两个 <button> 不能包在 label 内，否则点按钮会连带切换勾选 -->
        <div class="auth-agree">
          <label class="check">
            <input id="reg-agree" v-model="agree" type="checkbox" aria-label="我已阅读并同意《用户协议》与《隐私政策》" />
            <span class="box"></span>
          </label>
          <!-- 书名号与汉字之间不留空格：模板换行会被折叠成半角空格，在《》两侧造成肉眼可见
               的空隙（也与 aria-label 的无空格口径不一致），故三个节点写成同一行。 -->
          <span class="auth-agree-text">我已阅读并同意<button type="button" data-doc="agreement" @click="openDoc('agreement')">《用户协议》</button>与<button type="button" data-doc="privacy" @click="openDoc('privacy')">《隐私政策》</button></span>
        </div>
        <div
          id="reg-error-box"
          ref="regErrorBox"
          class="alert danger"
          role="alert"
          tabindex="-1"
          :hidden="!regError"
        >
          <span class="ico"><svg aria-hidden="true"><use href="#i-circle-alert" /></svg></span>
          <span class="body" id="reg-error-text">{{ regError }}</span>
        </div>
        <button id="register-btn" type="submit" class="btn btn--primary btn--block auth-submit" :disabled="regBusy">
          <span id="register-btn-text">注册并登录</span>
        </button>
      </form>
      <p class="auth-switch-hint">
        已有账号？
        <button type="button" data-auth-switch="login" @click="switchMode('login')">直接登录</button>
      </p>
    </div>
  </div>
</template>
