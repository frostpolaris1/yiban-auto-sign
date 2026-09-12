/* 用户端「账号与设置」页（/user）行为。
   依赖 core.js 的公开面：YB.api / YB.identity / YB.toast / YB.el / YB.confirmDialog /
   YB.openModal / YB.openConfirmPasswordModal / YB.doLogout / YB.passwordClasses / YB.PW_*。

   迁移自旧 user.html 的内联脚本，除日历外功能逐项保留（日历已拆到 /user/calendar）：
     · 调度模式提示条（/api/me 的 sign_order / sign_window / time_pref_allowed）
     · 我的账号列表：状态图标 + 语义状态行 + 审核徽章 + 暂停/恢复 + 编辑 + 软删除 + 撤销删除
     · 提交/编辑账号弹窗（编辑走 PUT，新建走 POST，支持清除已配置识别码）
     · 自选签到时段网格（拥挤度/裁剪/满员/禁用四态，clear 恢复自动分配）
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
  var editingIndex = null;      // null = 新建
  var clearCodeFlag = false;    // 标记清除已配置设备识别码
  var pauseBusy = false;        // 暂停/恢复的连点保护
  var submitting = false;       // 账号表单提交中
  var mailNotifyOn = true;
  var prefCollapsed = false;

  /* ---------------- 小工具 ---------------- */
  function iconUse(name) {
    return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  }
  var STATE_ICON = {
    success: "circle-check", already: "circle-check", no_task: "circle-minus",
    failed: "circle-x", retrying: "refresh-cw", skipped_window: "ban",
    skipped_norange: "ban", paused: "circle-pause", user_cancelled: "circle-stop"
  };
  function badge(text, variant) {
    return YB.el("span", { class: "badge" + (variant ? " " + variant : ""), text: text });
  }

  /* ---------------- 调度提示条 ---------------- */
  function renderScheduleBanner(me) {
    var bar = document.querySelector("[data-schedule-banner]");
    var txt = document.querySelector("[data-schedule-banner-text]");
    if (!bar || !txt) return;
    var order = me.sign_order === "random" ? "每天随机安排" : "按固定顺序安排";
    var pref = me.time_pref_allowed ? "可自选签到时间" : "暂不可自选签到时间";
    txt.textContent = "签到方式：" + order + " · 窗口 " + (me.sign_window || "") + " · " + pref;
    bar.hidden = false;
  }

  /* ---------------- 账号列表 ---------------- */
  function loadAccounts() {
    return YB.api("GET", "/api/my-accounts").then(function (data) {
      accounts = (data && data.accounts) || [];
      renderList();
    }).catch(function (e) { YB.toast.error(e.message); });
  }

  function stateLine(a) {
    var s = a.state_status || "pending";
    if (s === "success" || s === "already") return { cls: "state-line--ok", text: "今日已完成签到" };
    if (s === "no_task") return { cls: "state-line--muted", text: "今日无需签到" };
    if (s === "skipped_window" || s === "skipped_norange") return { cls: "state-line--warn", text: "未在签到时段" };
    if (s === "failed") {
      return { cls: "state-line--bad", text: "今日签到失败" + (a.state_message ? "：" + a.state_message : "") };
    }
    if (s === "paused") return { cls: "state-line--bad", text: "账号密码异常，已暂停签到，请修改密码" };
    if (s === "user_cancelled") return { cls: "state-line--bad", text: "已取消签到（可点「恢复签到」重新开启）" };
    if (s === "retrying") return { cls: "state-line--warn", text: "签到重试中" };
    // 待签：state_message 形如"计划 HH:MM"（自动错峰），其余情况不展示
    var plan = a.state_message && a.state_message.indexOf("计划") === 0 ? " · 今日" + a.state_message : "";
    return { cls: "state-line--muted", text: "待签到" + plan + " · 前方排队 " + a.queue_ahead + " 人" };
  }

  function stateAriaText(a) {
    if (a.deleted) return a.deleted_by_me ? "状态：已删除（7 天内可撤销）" : "状态：已被管理员删除";
    var s = a.state_status || "pending";
    var map = {
      success: "今日已完成签到", already: "今日已完成签到", no_task: "今日无需签到",
      skipped_window: "未在签到时段", skipped_norange: "未在签到时段",
      failed: "今日签到失败", paused: "账号密码异常，已暂停签到",
      user_cancelled: "已取消签到", retrying: "签到重试中"
    };
    return "状态：" + (map[s] || "待签到");
  }

  function actionButton(label, cls, onClick) {
    var b = YB.el("button", { type: "button", class: cls, text: label });
    b.addEventListener("click", onClick);
    return b;
  }

  function accountCard(a, i) {
    var card = YB.el("div", { class: "account-card" });
    var head = YB.el("div", { class: "account-head" });
    var ident = YB.el("div", { class: "account-ident" });

    var iconBox = YB.el("span", { class: "account-icon" });
    iconBox.innerHTML = iconUse(a.deleted ? "trash" : (STATE_ICON[a.state_status] || "clock"));
    ident.appendChild(iconBox);
    ident.appendChild(YB.el("span", { class: "sr-only", text: stateAriaText(a) }));

    var info = YB.el("div");
    info.appendChild(YB.el("div", { class: "account-name", text: a.display_name }));
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
    } else if (a.status === "active") {
      var line = stateLine(a);
      info.appendChild(YB.el("div", { class: "state-line " + line.cls, text: line.text }));
    }
    ident.appendChild(info);

    var actions = YB.el("div", { class: "account-actions" });
    if (a.deleted) {
      actions.appendChild(badge("已删除"));
      if (a.deleted_by_me) {
        actions.appendChild(actionButton("撤销删除", "btn btn--ghost btn--sm", function () { restoreAccount(i); }));
      } else {
        actions.appendChild(YB.el("span", { class: "account-note", text: "待管理员处理" }));
      }
    } else {
      if (a.user_paused) actions.appendChild(badge("已取消", "danger"));
      else actions.appendChild(statusBadge(a.status));
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

    if (a.status === "rejected") {
      var rej = YB.el("div", { class: "alert danger account-reject", role: "status" });
      rej.appendChild(YB.el("span", { class: "ico", html: iconUse("circle-alert") }));
      rej.appendChild(YB.el("span", {
        class: "body",
        text: "账号已被拒绝" + (a.reject_reason ? "：" + a.reject_reason : "") + "。修改后点「修改并重新提交」重新进入审核。"
      }));
      card.appendChild(rej);
    }
    if (a.logs && a.logs.length) {
      var det = YB.el("details", { class: "account-details" });
      det.appendChild(YB.el("summary", { text: "最近签到记录（" + a.logs.length + " 条）" }));
      det.appendChild(YB.el("pre", { class: "log-view", text: a.logs.join("\n") }));
      card.appendChild(det);
    }
    if (!a.deleted && a.status === "pending") {
      card.appendChild(YB.el("p", { class: "account-pending-hint", text: "审核通过后即可查看签到日历" }));
    }
    return card;
  }

  function actionLink(label, cls, href) {
    return YB.el("a", { class: cls, href: href, text: label });
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

  /* ---------------- 提交 / 编辑账号（模态） ---------------- */
  function inputField(o) {
    var field = YB.el("div", { class: "field" });
    var label = YB.el("label", { class: "field-label", for: o.id, text: o.label });
    if (o.required) {
      label.appendChild(YB.el("span", { class: "req", "aria-hidden": "true", text: "*" }));
    }
    var input = YB.el("input", {
      id: o.id, class: "input", type: o.type || "text",
      placeholder: o.placeholder || "", autocomplete: o.autocomplete || null
    });
    if (o.value) input.value = o.value;
    if (o.maxlength) input.maxLength = o.maxlength;
    if (o.required) input.required = true;
    field.appendChild(label);
    field.appendChild(input);
    if (o.help) field.appendChild(YB.el("p", { class: "field-help", text: o.help }));
    return { field: field, input: input };
  }

  function buildAccountForm(a) {
    var editing = !!a;
    var body = YB.el("div");
    body.appendChild(YB.el("p", {
      class: "panel-sub",
      text: editing
        ? "修改后需重新提交审核，审核通过即自动签到。"
        : "提交后等待管理员审核，审核通过即自动签到。每个用户限提交一个账号。"
    }));
    var err = YB.el("div", { class: "alert danger", role: "alert", hidden: true });
    err.appendChild(YB.el("span", { class: "ico", html: iconUse("circle-alert") }));
    var errText = YB.el("span", { class: "body" });
    err.appendChild(errText);
    body.appendChild(err);

    var stack = YB.el("div", { class: "form-stack" });
    var name = inputField({
      id: "f-name", label: "名称 / 备注（可选）", maxlength: 50,
      value: editing ? a.name : "", placeholder: "如：我的易班账号",
      help: "备注会显示给管理员，用于审核与定位签到问题。"
    });
    var phone = inputField({
      id: "f-phone", label: "易班手机号", required: true, maxlength: 20,
      value: editing ? a.phone : "", placeholder: "登录易班的手机号"
    });
    var password = inputField({
      id: "f-password", label: "易班密码", type: "password", required: !editing,
      placeholder: editing ? "留空表示不修改密码" : "用于自动登录签到",
      autocomplete: "new-password"
    });
    var model = inputField({
      id: "f-model", label: "设备型号（可选，不清楚就留空）", maxlength: 50,
      value: editing ? a.phone_model : "", placeholder: "按易班 App 设备绑定页填写",
      help: "仅在提示「请使用授权设备进行签到」时才需填写，不确定就留空。"
    });
    var code = inputField({
      id: "f-code", label: "设备识别码（可选，不清楚就留空）", maxlength: 100,
      placeholder: editing && a.has_phone_code ? "留空表示不修改（已配置）" : "64 位十六进制识别码"
    });
    var clearBtn = YB.el("button", {
      type: "button", class: "btn btn--ghost btn--sm", text: "清除已配置识别码",
      hidden: !(editing && a.has_phone_code)
    });
    clearBtn.addEventListener("click", function () {
      clearCodeFlag = !clearCodeFlag;
      if (clearCodeFlag) {
        code.input.value = "";
        code.input.readOnly = true;
        code.input.placeholder = "提交后将清除已配置识别码";
        clearBtn.textContent = "取消清除";
      } else {
        code.input.readOnly = false;
        code.input.placeholder = "64 位十六进制识别码";
        clearBtn.textContent = "清除已配置识别码";
      }
    });
    code.field.appendChild(clearBtn);

    [name, phone, password, model, code].forEach(function (f) { stack.appendChild(f.field); });
    body.appendChild(stack);

    return {
      node: body, name: name.input, phone: phone.input, password: password.input,
      model: model.input, code: code.input,
      showError: function (msg) { errText.textContent = msg; err.hidden = false; },
      hideError: function () { errText.textContent = ""; err.hidden = true; }
    };
  }

  function submitAccountForm(form) {
    if (submitting) return false;
    form.hideError();
    var phone = form.phone.value.trim();
    var password = form.password.value;
    if (!phone) { form.showError("请填写易班手机号"); form.phone.focus(); return false; }
    if (editingIndex === null && !password) { form.showError("请填写易班密码"); form.password.focus(); return false; }
    var payload = {
      name: form.name.value.trim(),
      phone: phone,
      password: password,
      phone_model: form.model.value.trim(),
      // 已标记清除 → 传 __clear__ 由后端清空；留空表示不修改
      phone_code: clearCodeFlag ? "__clear__" : form.code.value.trim()
    };
    submitting = true;
    var req = editingIndex === null
      ? YB.api("POST", "/api/my-accounts", payload)
      : YB.api("PUT", "/api/my-accounts/" + editingIndex, payload);
    req.then(function (data) {
      submitting = false;
      YB.closeModal();
      YB.toast.success(data.msg || "已保存");
      loadAccounts();
    }).catch(function (e) {
      submitting = false;
      form.showError(e.message || "保存失败，请稍后再试");
    });
    return false;   // 由请求结果决定是否关闭，校验/失败时保持打开
  }

  function openAccountForm(index) {
    editingIndex = typeof index === "number" ? index : null;
    clearCodeFlag = false;
    var a = editingIndex === null ? null : accounts[editingIndex];
    var form = buildAccountForm(a);
    YB.openModal({
      title: editingIndex === null ? "提交我的易班账号" : "编辑我的易班账号",
      body: form.node,
      actions: [
        { label: "取消", variant: "ghost" },
        {
          label: editingIndex === null ? "提交账号" : "保存修改", variant: "primary",
          onClick: function () { return submitAccountForm(form); }
        }
      ]
    });
  }

  /* ---------------- 自选签到时段 ---------------- */
  function loadTimePref(preserve) {
    var card = $("time-pref-card");
    YB.api("GET", "/api/my-time-pref").then(function (data) {
      if (!data.has_account) { card.hidden = true; return; }
      card.hidden = false;
      $("pref-window").textContent = data.window || "";
      renderPrefSlots(data);
      $("pref-disabled-hint").hidden = !!data.allowed;
      var est = $("pref-estimate");
      if (data.allowed && data.pref) {
        est.textContent = "";
        est.hidden = true;
      } else if (data.estimated) {
        est.textContent = "预计签到时段：" + data.estimated + (data.estimate_note || "")
          + (data.allowed ? "" : "（自选未开启，按自动分配）");
        est.hidden = false;
      } else {
        est.textContent = data.estimate_note || "";
        est.hidden = !data.estimate_note;
      }
      // 未开启时默认收起；修改后的局部刷新保留用户当前展开态，避免页面跳动
      if (!preserve) setPrefCollapsed(!data.allowed);
    }).catch(function () { card.hidden = true; });
  }

  function renderPrefSlots(data) {
    var grid = $("pref-slot-grid");
    grid.innerHTML = "";
    var tip = "";
    var slots = data.slots || [];
    slots.forEach(function (s, i) {
      var btn = YB.el("button", { type: "button" });
      if (s.disabled) {
        // 完全落入掐头去尾裁剪区：不可选
        btn.className = "slot slot--off";
        btn.disabled = true;
        btn.title = "该时段被掐头去尾保留，不可选择";
        btn.appendChild(YB.el("div", { class: "slot-name", text: s.label }));
        btn.appendChild(YB.el("div", { class: "slot-pct", text: "已保留" }));
        grid.appendChild(btn);
        return;
      }
      var sel = data.pref_slot === s.slot_min;
      var full = s.pct >= 100;          // 满员仍可选（先到先得 + 溢出顺延），用警示色提示
      var partial = !!s.edge_note;      // 部分落入裁剪区：虚线框，调度在可用部分执行
      btn.className = "slot" + (sel ? " slot--on" : full ? " slot--full" : partial ? " slot--partial" : "");
      if (partial) btn.title = s.edge_note + "，选中后将在可用部分为你签到";
      btn.appendChild(YB.el("div", { class: "slot-name", text: s.label }));
      btn.appendChild(YB.el("div", { class: "slot-pct", text: "已选" + s.pct + "%" }));
      btn.addEventListener("click", function () { pickTimePref(s.slot_min); });
      grid.appendChild(btn);
      // 首尾时段提醒（选中时）；部分裁剪的提示优先，未开启时与"暂不生效"拼接，两则信息都不丢
      if (sel && (i === 0 || i === slots.length - 1)) {
        var edgeTip = s.edge_note ? s.edge_note + "，选中后将在可用部分签到"
          : i === 0 ? "最早时段：窗口开始后最先为你签到"
            : "最后时段：临近窗口截止执行，网络波动可能导致错过";
        tip = (data.allowed ? "" : "未开启：") + edgeTip;
      }
    });
    var tipEl = $("pref-tip");
    tipEl.textContent = tip;
    tipEl.classList.toggle("state-line--warn", !!tip);
  }

  function setPrefCollapsed(collapsed) {
    prefCollapsed = collapsed;
    $("pref-body").hidden = collapsed;
    $("pref-collapse-btn").setAttribute("aria-expanded", String(!collapsed));
    $("pref-collapse-label").textContent = collapsed ? "未开启（展开预配置）" : "收起";
  }

  function pickTimePref(slot) {
    YB.api("PUT", "/api/my-time-pref", { slot_min: slot }).then(function (data) {
      YB.toast.success(data.msg || "已保存");
      loadTimePref(true);   // 局部刷新：保留展开态
    }).catch(function (e) { YB.toast.error(e.message); });
  }

  function clearTimePref() {
    YB.api("PUT", "/api/my-time-pref", { slot_min: null }).then(function (data) {
      YB.toast.success(data.msg || "已清除");
      loadTimePref(true);
    }).catch(function (e) { YB.toast.error(e.message); });
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
  function bindStatic() {
    var logout = document.querySelector("[data-user-logout]");
    if (logout) logout.addEventListener("click", function () { YB.doLogout(); });

    var openBtn = $("open-account-btn");
    if (openBtn) openBtn.addEventListener("click", function () { openAccountForm(); });

    var mail = $("mail-notify-switch");
    if (mail) mail.addEventListener("change", onMailNotifyChange);

    var collapseBtn = $("pref-collapse-btn");
    if (collapseBtn) collapseBtn.addEventListener("click", function () { setPrefCollapsed(!prefCollapsed); });
    var clearBtn = $("pref-clear-btn");
    if (clearBtn) clearBtn.addEventListener("click", clearTimePref);

    // 密码可见性切换（三个框共用）
    Array.prototype.forEach.call(document.querySelectorAll("[data-pw-toggle]"), function (btn) {
      btn.addEventListener("click", function () {
        var input = $(btn.getAttribute("data-pw-toggle"));
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
        btn.setAttribute("aria-pressed", String(show));
        btn.innerHTML = iconUse(show ? "eye-off" : "eye");
      });
    });

    $("password-form").addEventListener("submit", function (e) {
      e.preventDefault();
      var tip = $("p-tip");
      var np = $("p-new").value;
      if (np.length < YB.PW_MIN_LEN || YB.passwordClasses(np) < YB.PW_MIN_CLASSES) {
        tip.textContent = "";
        YB.toast.error("新密码" + YB.PW_POLICY_HINT);
        return;
      }
      if (np !== $("p-confirm").value) {
        tip.textContent = "";
        YB.toast.error("两次输入的新密码不一致");
        return;
      }
      tip.textContent = "提交中…";
      YB.api("POST", "/api/me/password", {
        old_password: $("p-old").value, new_password: np, confirm_password: $("p-confirm").value
      }).then(function (data) {
        tip.textContent = "";
        e.target.reset();
        YB.toast.success(data.msg || "密码已更新");
      }).catch(function (err) {
        tip.textContent = "";
        YB.toast.error(err.message);
      });
    });

    var del = document.querySelector("[data-delete-account]");
    if (del) del.addEventListener("click", onDeleteAccount);
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    bindStatic();
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
      loadTimePref();
    }).catch(function () { location.href = YB.BASE + "/login"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
