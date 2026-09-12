/* 用户端「账号与设置」页（/user）行为。
   依赖 core.js 的公开面：YB.api / YB.identity / YB.toast / YB.el / YB.confirmDialog /
   YB.openConfirmPasswordModal / YB.doLogout / YB.passwordClasses / YB.PW_*。
   账号表单与自选时段网格复用共享组件（components/account-form.js、components/time-pref.js），
   本文件只保留本页特有编排：账号卡列表、暂停/撤销删除、邮件开关、改密弹窗、注销。

   迁移自旧 user.html 的内联脚本，除日历外功能逐项保留（日历已拆到 /user/calendar）：
     · 调度模式提示条（/api/me 的 sign_order / sign_window / time_pref_allowed）
     · 我的账号列表：状态图标 + 语义状态行 + 审核徽章 + 暂停/恢复 + 编辑 + 软删除 + 撤销删除
     · 提交/编辑账号弹窗（共享组件；编辑走 PUT，新建走 POST，支持清除已配置识别码）
     · 自选签到时段网格（共享组件；拥挤度/裁剪/满员/禁用四态，clear 恢复自动分配）
     · 邮件提醒开关（失败回滚）
     · 修改密码（口令策略与后端同一口径）
     · 注销账号（两次确认：宽限期说明 → 密码确认）
   全部动态文本一律经 textContent / YB.el 写入，不用 innerHTML 拼不可信数据。 */
(function () {
  "use strict";

  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;

  var accounts = [];
  var pauseBusy = false;        // 暂停/恢复的连点保护
  var mailNotifyOn = true;
  var pref = null;              // 共享时段网格控制器

  /* ---------------- 小工具 ---------------- */
  function iconUse(name) {
    return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  }
  // 账号卡的图标表达**审核状态**（签到状态已迁到「签到日历」页，本页只谈账号本身）
  var AUDIT_ICON = { pending: "clock", rejected: "circle-x", active: "circle-check" };
  function badge(text, variant) {
    return YB.el("span", { class: "badge" + (variant ? " " + variant : ""), text: text });
  }

  /* ---------------- 调度提示条 ---------------- */
  function renderScheduleBanner(me) {
    var bar = document.querySelector("[data-schedule-banner]");
    var txt = document.querySelector("[data-schedule-banner-text]");
    if (!bar || !txt) return;
    var order = me.sign_order === "random" ? "每天随机安排" : "按固定顺序安排";
    var prefText = me.time_pref_allowed ? "可自选签到时间" : "暂不可自选签到时间";
    txt.textContent = "签到方式：" + order + " · 窗口 " + (me.sign_window || "") + " · " + prefText;
    bar.hidden = false;
  }

  /* ---------------- 账号列表 ---------------- */
  function loadAccounts() {
    return YB.api("GET", "/api/my-accounts").then(function (data) {
      accounts = (data && data.accounts) || [];
      renderList();
    }).catch(function (e) { YB.toast.error(e.message); });
  }

  function auditAriaText(a) {
    if (a.deleted) return a.deleted_by_me ? "状态：已删除（7 天内可撤销）" : "状态：已被管理员删除";
    if (a.user_paused) return "状态：已取消（可恢复签到）";
    var map = { pending: "待审核", rejected: "已拒绝", active: "已生效" };
    return "状态：" + (map[a.status] || "未知");
  }

  function actionButton(label, cls, onClick) {
    var b = YB.el("button", { type: "button", class: cls, text: label });
    b.addEventListener("click", onClick);
    return b;
  }

  function actionLink(label, cls, href) {
    return YB.el("a", { class: cls, href: href, text: label });
  }

  function accountCard(a, i) {
    var card = YB.el("div", { class: "account-card" });
    var head = YB.el("div", { class: "account-head" });
    var ident = YB.el("div", { class: "account-ident" });

    var iconBox = YB.el("span", { class: "account-icon" });
    iconBox.innerHTML = iconUse(a.deleted ? "trash" : (AUDIT_ICON[a.status] || "clock"));
    ident.appendChild(iconBox);
    ident.appendChild(YB.el("span", { class: "sr-only", text: auditAriaText(a) }));

    var info = YB.el("div");
    // 名称 + 审核徽章同一行：徽章不再占用操作行，竖屏下按钮才排得下
    var titleRow = YB.el("div", { class: "account-title-row" });
    titleRow.appendChild(YB.el("span", { class: "account-name", text: a.display_name }));
    if (a.deleted) titleRow.appendChild(badge("已删除"));
    else if (a.user_paused) titleRow.appendChild(badge("已取消", "danger"));
    else titleRow.appendChild(statusBadge(a.status));
    info.appendChild(titleRow);
    info.appendChild(YB.el("div", {
      class: "account-meta",
      text: String(a.phone || "") + (a.phone_model ? " · " + a.phone_model : "")
    }));
    if (a.deleted) {
      info.appendChild(YB.el("div", {
        class: "account-note",
        text: a.deleted_by_me
          ? "你已删除此账号，7 天内可撤销恢复，超期自动清除"
          : "已被管理员删除，待管理员处理"
      }));
    }
    ident.appendChild(info);

    var actions = YB.el("div", { class: "account-actions" });
    if (a.deleted) {
      if (a.deleted_by_me) {
        actions.appendChild(actionButton("撤销删除", "btn btn--ghost btn--sm", function () { restoreAccount(i); }));
      } else {
        actions.appendChild(YB.el("span", { class: "account-note", text: "待管理员处理" }));
      }
    } else {
      if (a.status === "active") {
        actions.appendChild(actionLink("签到日历", "btn btn--ghost btn--sm", YB.BASE + "/user/calendar"));
        if (!a.pause_forbidden) {
          actions.appendChild(actionButton(
            a.user_paused ? "恢复签到" : "暂停签到",
            "btn btn--ghost btn--sm",
            function () { togglePause(i); }
          ));
        }
      }
      actions.appendChild(actionButton(
        a.status === "rejected" ? "修改并重新提交" : "编辑",
        "btn btn--ghost btn--sm",
        function () { openAccountForm(i); }
      ));
      actions.appendChild(actionButton("删除", "btn btn--ghost btn--danger-ghost btn--sm", function () { deleteAccount(i); }));
    }
    head.appendChild(ident);
    head.appendChild(actions);
    card.appendChild(head);

    // 只保留"需要用户本人处理"的异常提示（例行签到状态属于「签到日历」页）
    if (a.status === "rejected") {
      var rej = YB.el("div", { class: "alert danger account-reject", role: "status" });
      rej.appendChild(YB.el("span", { class: "ico", html: iconUse("circle-alert") }));
      rej.appendChild(YB.el("span", {
        class: "body",
        text: "账号已被拒绝" + (a.reject_reason ? "：" + a.reject_reason : "") + "。修改后点「修改并重新提交」。"
      }));
      card.appendChild(rej);
    }
    if (!a.deleted && a.status === "active" && a.state_status === "paused") {
      var bad = YB.el("div", { class: "alert danger account-reject", role: "status" });
      bad.appendChild(YB.el("span", { class: "ico", html: iconUse("circle-alert") }));
      bad.appendChild(YB.el("span", { class: "body", text: "账号密码异常，签到已暂停，请编辑账号更新密码。" }));
      card.appendChild(bad);
    }
    if (a.logs && a.logs.length) {
      var det = YB.el("details", { class: "account-details" });
      det.appendChild(YB.el("summary", { text: "最近签到记录（" + a.logs.length + " 条）" }));
      det.appendChild(YB.el("pre", { class: "log-view", text: a.logs.join(String.fromCharCode(10)) }));
      card.appendChild(det);
    }
    if (!a.deleted && a.status === "pending") {
      card.appendChild(YB.el("p", { class: "account-pending-hint", text: "审核通过后自动签到，结果见「签到日历」。" }));
    }
    return card;
  }

  function renderList() {
    var list = $("account-list");
    list.innerHTML = "";
    $("accounts-empty").hidden = accounts.length > 0;
    // 还有未删除账号时隐藏入口；全部被删除时保留（软删除不死路）
    $("open-account-btn").hidden = accounts.some(function (a) { return !a.deleted; });
    accounts.forEach(function (a, i) { list.appendChild(accountCard(a, i)); });
  }

  function statusBadge(status) {
    if (status === "pending") return badge("待审核", "warning");
    if (status === "rejected") return badge("已拒绝", "danger");
    if (status === "active") return badge("已生效", "success");
    return badge(String(status == null ? "" : status));
  }

  function togglePause(i) {
    if (pauseBusy) return;
    var a = accounts[i];
    var next = !a.user_paused;
    var ask = next
      ? YB.confirmDialog({
          title: "暂停签到",
          body: "确定暂停「" + a.display_name + "」的签到吗？暂停后系统将不再自动签到，可随时恢复。",
          confirmText: "暂停签到", danger: true
        })
      : Promise.resolve(true);
    ask.then(function (ok) {
      if (!ok) return;
      pauseBusy = true;
      YB.api("PUT", "/api/my-accounts/" + i + "/pause", { paused: next }).then(function (data) {
        YB.toast.success(data.msg || (next ? "已暂停" : "已恢复"));
        loadAccounts();
      }).catch(function (e) { YB.toast.error(e.message); })
        .then(function () { pauseBusy = false; });
    });
  }

  function deleteAccount(i) {
    var a = accounts[i];
    YB.confirmDialog({
      title: "删除账号",
      body: "确定删除「" + a.display_name + "」(" + a.phone + ") 吗？删除后 7 天内可撤销恢复，超过 7 天将自动清除。",
      confirmText: "删除", danger: true
    }).then(function (ok) {
      if (!ok) return;
      YB.api("DELETE", "/api/my-accounts/" + i).then(function () {
        YB.toast.success("已删除，7 天内可撤销");
        loadAccounts();
      }).catch(function (e) { YB.toast.error(e.message); });
    });
  }

  function restoreAccount(i) {
    var a = accounts[i];
    YB.confirmDialog({
      title: "撤销删除",
      body: "撤销删除「" + a.display_name + "」(" + a.phone + ")？将恢复到删除前的状态。",
      confirmText: "撤销删除"
    }).then(function (ok) {
      if (!ok) return;
      YB.api("POST", "/api/my-accounts/" + i + "/restore", {}).then(function () {
        YB.toast.success("已恢复");
        loadAccounts();
      }).catch(function (e) { YB.toast.error(e.message); });
    });
  }

  /* ---------------- 提交 / 编辑账号（共享组件） ---------------- */
  function openAccountForm(index) {
    var editing = typeof index === "number";
    YB.accountForm.open({
      variant: "user",
      index: editing ? index : null,
      account: editing ? accounts[index] : null,
      endpoints: { create: "/api/my-accounts", update: "/api/my-accounts/" },
      lockButton: false,           // 用户端沿用原实现：仅用在途标志防连点，不改按钮
      onSaved: function () { loadAccounts(); }
    });
  }

  /* ---------------- 邮件提醒 ---------------- */
  function renderMailNotify() {
    var el = $("mail-notify-switch");
    if (el) el.checked = mailNotifyOn;
  }

  function onMailNotifyChange() {
    mailNotifyOn = $("mail-notify-switch").checked;
    var tip = $("mail-notify-tip");
    tip.textContent = "保存中…";
    YB.api("PUT", "/api/my-mail-notify", { enabled: mailNotifyOn }).then(function () {
      tip.textContent = mailNotifyOn ? "已开启：签到失败时将邮件提醒你" : "已关闭：不再发送签到失败邮件";
      setTimeout(function () { tip.textContent = ""; }, 3000);
    }).catch(function (e) {
      mailNotifyOn = !mailNotifyOn;   // 保存失败回滚开关
      $("mail-notify-switch").checked = mailNotifyOn;
      tip.textContent = "保存失败：" + e.message;
    });
  }

  /* ---------------- 注销账号 ---------------- */
  function onDeleteAccount() {
    YB.confirmDialog({
      title: "注销账号",
      body: "注销将删除你的账号、易班账号与自选签到时间。7 天内可撤销恢复，超过 7 天将永久删除，无法找回。",
      confirmText: "继续注销", danger: true
    }).then(function (ok) {
      if (!ok) return;
      YB.openConfirmPasswordModal(
        "注销后账号将无法登录，易班账号与自选签到时间会被删除；7 天宽限期内可撤销。请输入当前密码完成注销。",
        function (pw) {
          YB.api("POST", "/api/me/delete", { password: pw }).then(function (data) {
            YB.toast.success(data.msg || "账号已注销");
            try { localStorage.clear(); } catch (e) { /* 受限环境忽略 */ }
            setTimeout(function () { location.href = YB.BASE + "/login"; }, 1200);
          }).catch(function (e) {
            // 文案由后端给出（400 密码不正确 / 403 / 429 请稍后再试 / 500）
            YB.toast.error(e.message || "注销失败，请稍后再试");
          });
        }
      );
    });
  }

  /* ---------------- 静态控件绑定 ---------------- */
  // 密码可见性切换：把三个框共用的切换逻辑抽出来，绑定在容器上
  function bindPwToggle(root) {
    Array.prototype.forEach.call(root.querySelectorAll("[data-pw-toggle]"), function (btn) {
      btn.addEventListener("click", function () {
        var input = $(btn.getAttribute("data-pw-toggle"));
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
        btn.setAttribute("aria-pressed", String(show));
        btn.innerHTML = iconUse(show ? "eye-off" : "eye");
      });
    });
  }

  function bindStatic() {
    var logout = document.querySelector("[data-user-logout]");
    if (logout) logout.addEventListener("click", function () { YB.doLogout(); });

    var openBtn = $("open-account-btn");
    if (openBtn) openBtn.addEventListener("click", function () { openAccountForm(); });

    var mail = $("mail-notify-switch");
    if (mail) mail.addEventListener("change", onMailNotifyChange);

    bindPwToggle(document);

    var del = document.querySelector("[data-delete-account]");
    if (del) del.addEventListener("click", onDeleteAccount);

    var pwEntry = $("password-modal-btn");
    if (pwEntry) pwEntry.addEventListener("click", openPasswordModal);
  }

  /* ---------------- 修改密码（弹窗，表单本体在模板 <template> 内） ---------------- */
  function pwError(form, msg) {
    var box = form.querySelector(".alert.danger");
    if (!box) return;
    box.hidden = !msg;
    var body = box.querySelector(".body");
    if (body) body.textContent = msg || "";
  }

  function submitPasswordForm(form) {
    var np = form.querySelector("#p-new").value;
    if (np.length < YB.PW_MIN_LEN || YB.passwordClasses(np) < YB.PW_MIN_CLASSES) {
      pwError(form, "新密码" + YB.PW_POLICY_HINT);
      return false;
    }
    if (np !== form.querySelector("#p-confirm").value) {
      pwError(form, "两次输入的新密码不一致");
      return false;
    }
    pwError(form, "");
    YB.api("POST", "/api/me/password", {
      old_password: form.querySelector("#p-old").value,
      new_password: np,
      confirm_password: np
    }).then(function (data) {
      YB.closeModal();
      YB.toast.success(data.msg || "密码已更新");
    }).catch(function (err) {
      // 错误就显示在弹窗里（toast 的层级低于模态，用户看不到）
      pwError(form, err.message || "修改失败，请稍后再试");
    });
    return false;   // 由请求结果决定是否关闭
  }

  function openPasswordModal() {
    var tpl = $("tpl-password-form");
    if (!tpl) return;
    var wrap = YB.el("div");
    wrap.appendChild(tpl.content.cloneNode(true));
    var form = wrap.querySelector("form");
    bindPwToggle(form);
    // 回车提交（弹窗底部的主按钮由 core.js 渲染，这里兜住表单自身的 submit）
    form.addEventListener("submit", function (e) { e.preventDefault(); });
    YB.openModal({
      title: "修改密码",
      subtitle: "账号（注册邮箱）不可修改，改后下次登录使用新密码。",
      body: wrap,
      actions: [
        { label: "取消", variant: "ghost" },
        { label: "保存新密码", variant: "primary", onClick: function () { return submitPasswordForm(form); } }
      ]
    });
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    bindStatic();
    pref = YB.timePref.mount();
    YB.identity().then(function (me) {
      if (!me) { location.href = YB.BASE + "/login"; return; }
      if (me.role !== "user") { location.href = YB.BASE + "/"; return; }  // 管理员回后台
      var email = me.email || "";
      // 侧栏账号区只显示邮箱前缀，完整地址放 title（窄栏不撑破）
      Array.prototype.forEach.call(document.querySelectorAll("[data-account-email]"), function (n) {
        n.textContent = email.split("@")[0];
        n.title = email;
      });
      mailNotifyOn = !!me.mail_notify;
      renderMailNotify();
      renderScheduleBanner(me);
      loadAccounts();
      pref.load();
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
