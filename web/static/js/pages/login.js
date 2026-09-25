/* 登录 / 注册页行为。
   依赖 core.js 的公开面（YB.api / YB.openModal / YB.passwordClasses / YB.PW_MIN_LEN /
   YB.PW_MIN_CLASSES / YB.PW_POLICY_HINT / YB.BASE / YB.iconEl），口令判定与模态无障碍逻辑
   一律复用共享实现，本文件不再各自维护一份。本文件**不用** YB.escapeHtml：向页面写内容只走
   textContent 与 cloneNode，全文无 innerHTML 赋值，因此不存在需要转义的拼接面。

   功能清单（迁移自旧栈 login.html，逐项保留）：
   · 登录 / 注册 tab 切换（含切换淡入、aria-selected 同步、注册暂停时禁止切入）
   · 注册暂停态探测（GET /api/registration_paused）
   · 三个密码框的可见性切换
   · 注册表单字段级即时校验（邮箱格式/长度、口令策略、两次一致）
   · 登录提交（含注销冷静期 recoverable 分支 → 显形恢复按钮）
   · 注销冷静期恢复（POST /api/me/restore）
   · 注册提交（POST /api/register 后自动登录）
   · 用户协议 / 隐私政策模态（正文来自服务端渲染的 <template>）
   · 更新日志模态（core.js 的 openChangelog，页脚版本号触发）
*/
(function () {
  "use strict";

  var YB = window.YB;
  var $ = function (id) { return document.getElementById(id); };

  /* ---------------- tab 切换 ---------------- */
  var mode = "login";

  function switchMode(m) {
    // 注册暂停时禁止切入注册表单（tab 已禁用，此处兜底键盘/脚本触发路径）
    var regTab = $("tab-register");
    if (m === "register" && regTab && regTab.disabled) return;
    mode = m;
    // 整组显隐（表单 + 该模式下的切换提示）：只切表单会让另一个模式的提示残留，
    // 出现「表单已隐藏、提示还在」的双提示。
    $("login-pane").hidden = m !== "login";
    $("register-pane").hidden = m !== "register";
    var titles = { login: ["登录", "使用注册邮箱进入签到管理面板。"],
                   register: ["注册", "创建账号后即可添加并管理易班签到。"] };
    $("auth-title").textContent = titles[m][0];
    $("auth-sub").textContent = titles[m][1];
    for (var i = 0; i < 2; i++) {
      var btn = i === 0 ? $("tab-login") : regTab;
      var on = btn.getAttribute("data-auth-tab") === m;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", String(on));
    }
    // 切换淡入：移除类 → 强制回流 → 加回，重触发动画（与减弱动效规则见 app.css）
    var form = $(m === "login" ? "login-form" : "register-form");
    form.classList.remove("tab-enter");
    void form.offsetWidth;
    form.classList.add("tab-enter");
  }

  /* ---------------- 注册暂停 ---------------- */
  // 仅按公开布尔渲染 UI；真正的拦截在后端 /api/register（403），此处被绕过也无效。
  function applyRegistrationPause() {
    YB.api("GET", "/api/registration_paused").then(function (data) {
      if (!data || !data.paused) return;
      var rt = $("tab-register");
      if (rt) {
        rt.disabled = true;
        rt.title = "注册已暂停";
      }
      // 表单下方的「创建账号」提示同样要禁用，否则用户会被引导到打不开的注册表单
      Array.prototype.forEach.call(
        document.querySelectorAll('[data-auth-switch="register"]'),
        function (b) { b.disabled = true; b.title = "注册已暂停"; }
      );
      var hint = $("register-paused-hint");
      if (hint) hint.hidden = false;
    }).catch(function () { /* 状态获取失败不阻断登录页；后端注册 API 仍会拦截 */ });
  }

  /* ---------------- 错误展示 ---------------- */
  function showError(box, msg) {
    var text = box.querySelector(".body");
    if (text) text.textContent = msg;
    box.hidden = false;
    box.focus();  // 配合 tabindex="-1"，读屏即时播报
  }
  function hideError(box) { box.hidden = true; }

  /* ---------------- 字段级即时校验 ---------------- */
  var EMAIL_RE = /^[\w.+-]{1,32}@[\w-]+(\.[\w-]+)+$/;  // @ 前限 32 字符
  var regEmail = $("reg-email"), regEmailErr = $("reg-email-error");
  var regPw = $("reg-password"), regPwErr = $("reg-password-error");
  var regPw2 = $("reg-password2"), regPw2Err = $("reg-password2-error");

  function markField(input, errEl, invalid) {
    input.classList.toggle("is-invalid", invalid);
    if (errEl) errEl.hidden = !invalid;
  }
  // 两次密码一致性：确认框为空时不提示（提交由 required 兜底），非空即实时比对
  function validatePwMatch() {
    markField(regPw2, regPw2Err, regPw2.value !== "" && regPw2.value !== regPw.value);
  }

  regEmail.addEventListener("input", function () {
    var v = regEmail.value.trim();
    var invalid = v !== "" && !(EMAIL_RE.test(v) && v.length <= 64);
    if (v.indexOf("@") !== -1 && v.split("@")[0].length > 32) {
      regEmailErr.textContent = "邮箱用户名部分过长（最多 32 字符）";
    } else {
      regEmailErr.textContent = "邮箱格式不正确（示例：name@example.com）";
    }
    markField(regEmail, regEmailErr, invalid);
  });

  regPw.addEventListener("input", function () {
    var v = regPw.value;
    var n = v.length;
    var classes = YB.passwordClasses(v);
    regPwErr.textContent = "密码" + YB.PW_POLICY_HINT + "（当前 " + n + " 位，" + classes + " 类）";
    markField(regPw, regPwErr, n < YB.PW_MIN_LEN || classes < YB.PW_MIN_CLASSES);
    validatePwMatch();
  });

  regPw2.addEventListener("input", validatePwMatch);

  /* ---------------- 密码可见性切换 ---------------- */
  Array.prototype.forEach.call(document.querySelectorAll("[data-pw-toggle]"), function (btn) {
    btn.addEventListener("click", function () {
      var input = $(btn.getAttribute("data-pw-toggle"));
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
      btn.setAttribute("aria-pressed", String(show));
      while (btn.firstChild) btn.removeChild(btn.firstChild);
      btn.appendChild(YB.iconEl(show ? "eye-off" : "eye"));
    });
  });

  /* ---------------- tab 点击绑定 ---------------- */
  $("tab-login").addEventListener("click", function () { switchMode("login"); });
  $("tab-register").addEventListener("click", function () { switchMode("register"); });
  // 表单下方的切换提示（《创建账号》/《直接登录》）走同一 switchMode，
  // 但用独立属性 data-auth-switch 标记：它们不是 role=tab，不参与 tab 的选中态切换。
  Array.prototype.forEach.call(document.querySelectorAll("[data-auth-switch]"), function (btn) {
    btn.addEventListener("click", function () { switchMode(btn.getAttribute("data-auth-switch")); });
  });

  /* ---------------- 登录 ---------------- */
  $("login-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var errorBox = $("error-box");
    hideError(errorBox);
    var btn = $("login-btn");
    btn.disabled = true;
    YB.api("POST", "/api/login", {
      username: $("username").value,
      password: $("password").value
    }).then(function (data) {
      // 冷静期恢复分支必须先于 data.ok 判断：recoverable 响应同时带 ok:true，
      // 先查 ok 会误走普通登录跳转，此分支即成死代码。
      if (data && data.recoverable) {
        showError(errorBox, data.msg || "账号已注销，7 天内可恢复");
        $("restore-btn").hidden = false;
        return;
      }
      if (data && data.ok) {
        location.href = YB.BASE + (data.role === "admin" ? "/data/dashboard" : "/user/calendar");
        return;
      }
      showError(errorBox, (data && data.error) || "登录失败，请重试");
    }).catch(function (err) {
      showError(errorBox, (err && err.message) || "网络异常，请检查网络后重试");
    }).then(function () { btn.disabled = false; });
  });

  /* ---------------- 注销冷静期恢复 ---------------- */
  $("restore-btn").addEventListener("click", function () {
    var btn = $("restore-btn");
    var errorBox = $("error-box");
    btn.disabled = true;
    YB.api("POST", "/api/me/restore", {
      email: $("username").value.trim(),
      password: $("password").value
    }).then(function (data) {
      if (data && data.ok) {
        location.href = YB.BASE + (data.role === "admin" ? "/data/dashboard" : "/user/calendar");
        return;
      }
      showError(errorBox, (data && data.error) || "恢复失败，请稍后再试");
      btn.hidden = true;
    }).catch(function (err) {
      showError(errorBox, (err && err.message) || "网络异常，请检查网络后重试");
    }).then(function () { btn.disabled = false; });
  });

  /* ---------------- 注册 ---------------- */
  $("register-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var errorBox = $("reg-error-box");
    hideError(errorBox);
    var btn = $("register-btn");
    btn.disabled = true;

    var email = regEmail.value.trim();
    var password = regPw.value;
    // 必须勾选同意用户协议与隐私政策
    if (!$("reg-agree").checked) {
      showError(errorBox, "请先阅读并同意《用户协议》和《隐私政策》");
      btn.disabled = false;
      return;
    }
    // 提交前兜底：字段级即时校验优先（错误显示在字段下方，不弹顶部错误框）
    // 与后端 _password_policy_error 对齐：至少 10 位 + 四类中至少两类
    var emailInvalid = !(EMAIL_RE.test(email) && email.length <= 64);
    markField(regEmail, regEmailErr, emailInvalid);
    var pwClasses = YB.passwordClasses(password);
    var pwInvalid = password.length < YB.PW_MIN_LEN || pwClasses < YB.PW_MIN_CLASSES;
    markField(regPw, regPwErr, pwInvalid);
    var pwMismatch = regPw2.value !== password;
    markField(regPw2, regPw2Err, pwMismatch);
    if (emailInvalid || pwInvalid || pwMismatch) {
      btn.disabled = false;
      return;
    }

    YB.api("POST", "/api/register", { email: email, password: password, agree: true })
      .then(function (data) {
        return YB.api("POST", "/api/login", { username: email, password: password })
          .then(function (login) {
            if (login && login.ok) {
              location.href = YB.BASE + "/user/calendar";   // 注册即登录成功：同样落在日历页
              return;
            }
            showError(errorBox, "注册成功，但自动登录失败，请手动登录");
          });
      })
      .catch(function (err) {
        showError(errorBox, (err && err.message) || "网络异常，请检查网络后重试");
      })
      .then(function () { btn.disabled = false; });
  });

  /* ---------------- 协议 / 隐私模态 ---------------- */
  // 正文取自服务端渲染的 <template>（惰性内容），克隆后交给 core.js 的模态管理器，
  // 由它统一处理 Esc、Tab 焦点圈定、滚动锁与焦点归还。
  function openDoc(kind) {
    var tpl = $("doc-" + kind);
    if (!tpl) return;
    var title = kind === "agreement" ? "用户协议" : "隐私政策";
    var body = document.createElement("div");
    body.className = "md-body";  // 文档排印类（见 app.css 第 17 节）
    body.appendChild(tpl.content.cloneNode(true));
    YB.openModal({
      title: title,
      size: "lg",
      body: body,
      actions: [{ label: "我知道了", variant: "primary" }]
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-doc]"), function (btn) {
    btn.addEventListener("click", function () { openDoc(btn.getAttribute("data-doc")); });
  });

  /* ---------------- 初始化 ---------------- */
  // 统一线性淡入：与管理端 .tab-enter 一致；「减弱动态效果」下只淡入不位移（见 app.css）
  $("login-form").classList.add("tab-enter");
  applyRegistrationPause();
})();
