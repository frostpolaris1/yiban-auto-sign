/* 账号新增/编辑弹窗（用户端与管理端共用的唯一实现）。
   挂载到 window.YB.accountForm；classic script，仅向 YB 命名空间暴露 open()。

   调用方（pages/user_account.js、pages/work_accounts.js）只提供差异选项：
     variant          "user" | "admin"（决定字段文案与默认标题）
     account          列表项（admin 列表的 phone 已脱敏，编辑时值即脱敏号）
     index            编辑下标；null=新增
     endpoints        { create, update }，update 为前缀（后接 index）
     detail           编辑时是否先取 GET /api/accounts/<idx>/detail（admin 需要完整手机号与乐观锁快照）
     allowEmail       admin 新增时可绑定已注册用户或手填邮箱（下拉走 YB.selectField 自研
                      listbox：隐藏 input 保留 id/值契约，面板在模态内由组件 portal 到 body 防裁剪）
     onSaved(data)    保存成功回调（页面据此重新拉列表）
     lockButton       提交期间是否禁用主按钮（用户端沿用原实现不禁用，默认 true）

   安全：手机号完整值只经 input.dataset.full 暂存（按需取、提交即用），
   不写入 DOM 常驻文本、不写日志；所有动态文本走 YB.el 的 text/属性通道。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var CODE_CLEAR = "__clear__";
  var busy = false;

  // 用户端文案沿用已确认版本；管理端沿用旧弹窗（P4 已随旧栈退役）的既有口径。
  var TEXTS = {
    user: {
      nameLabel: "名称 / 备注（可选）", namePlaceholder: "如：我的易班账号",
      nameHelp: "会显示给管理员，便于审核。",
      phoneLabel: "易班手机号", phonePlaceholder: "登录易班的手机号",
      passwordLabel: "易班密码", passwordNewPlaceholder: "用于自动登录签到",
      modelLabel: "设备型号（可选，不清楚就留空）", modelHelp: "仅在提示「请使用授权设备」时填写，不确定就留空。",
      codeLabel: "设备识别码（可选，不清楚就留空）", codeHelp: "仅在提示「请使用授权设备」时填写，不确定就留空。",
      phoneEditHelp: ""
    },
    admin: {
      nameLabel: "名称（可选，不填显示为 账号N）", namePlaceholder: "如：武陵123庄方宜",
      nameHelp: "不填显示为 账号N。",
      phoneLabel: "手机号", phonePlaceholder: "易班登录手机号",
      passwordLabel: "密码", passwordNewPlaceholder: "易班登录密码",
      modelLabel: "设备型号（学校开启设备绑定时建议填写）", modelHelp: "",
      codeLabel: "设备识别码（学校开启设备绑定时建议填写）", codeHelp: "",
      phoneEditHelp: "为保护隐私，手机号已打码显示；如需修改请填写完整新号码"
    }
  };

  function svg(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }

  function labelFor(id, text, required) {
    var l = YB.el("label", { class: "field-label", for: id, text: text });
    if (required) l.appendChild(YB.el("span", { class: "req", "aria-hidden": "true", text: "*" }));
    return l;
  }
  function textInput(id, o) {
    o = o || {};
    var input = YB.el("input", {
      id: id, class: "input", type: o.type || "text",
      placeholder: o.placeholder || null,
      autocomplete: o.autocomplete || null,
      inputmode: o.inputmode || null
    });
    if (o.maxlength) input.maxLength = o.maxlength;
    if (o.required) input.required = true;
    if (o.value != null) input.value = o.value;
    return input;
  }
  // 字段顺序：标签 → 控件 → 可选元素（清除按钮）→ 帮助。帮助排在清除按钮之后，
  // 与用户端既有弹窗一致（按钮紧贴输入框，说明位于最末）。
  function field(labelText, input, required, help, beforeHelp) {
    var f = YB.el("div", { class: "field" });
    f.appendChild(labelFor(input.id, labelText, required));
    f.appendChild(input);
    if (beforeHelp) f.appendChild(beforeHelp);
    if (help) f.appendChild(YB.el("p", { class: "field-help", text: help }));
    return f;
  }

  /* ---------------- 表单结构 ---------------- */
  function build(opts, editing, a) {
    var T = TEXTS[opts.variant === "admin" ? "admin" : "user"];
    var body = YB.el("div");
    var err = YB.el("div", { class: "alert danger", role: "alert", hidden: true });
    err.appendChild(YB.el("span", { class: "ico", html: svg("circle-alert") }));
    var errText = YB.el("span", { class: "body" });
    err.appendChild(errText);
    body.appendChild(err);

    var stack = YB.el("div", { class: "form-stack" });
    var name = textInput("af-name", {
      maxlength: 50, value: editing ? a.name : "", placeholder: T.namePlaceholder
    });
    stack.appendChild(field(T.nameLabel, name, false, T.nameHelp));

    var email = null, manual = null, initial = null, manualWrap = null;
    if (opts.allowEmail && !editing) {
      /* 绑定用户下拉：自研 listbox（原生 select 面板样式不可控）。结构对齐
         work_settings.html 的静态用法——root[data-select-field] 内是隐藏 input（原
         #af-email，值契约不变，提交读取零改动）+ 空面板；触发器由 select-field.mount() 补。
         固定两目 + 分组头先就位，用户列表由 loadAvailableUsers 异步灌入（见下）。 */
      email = YB.el("input", { type: "hidden", id: "af-email", value: "" });
      var emailRoot = YB.el("div", { class: "select-field", "data-select-field": "af-email" });
      emailRoot.appendChild(email);
      emailRoot.appendChild(YB.el("div", { class: "select-menu", role: "listbox", hidden: true }));
      stack.appendChild(field("绑定用户（可选，绑定后进入待审核）", emailRoot, false, null));

      manual = textInput("af-email-manual", {
        type: "email", maxlength: 64, placeholder: "输入未注册邮箱（自动注册并进入待审核）"
      });
      initial = textInput("af-initial-password", {
        type: "password", maxlength: 64, autocomplete: "new-password",
        placeholder: "初始密码（至少 10 位）"
      });
      manualWrap = YB.el("div", { class: "form-stack", hidden: true });
      manualWrap.appendChild(field("手填邮箱", manual, true, null));
      manualWrap.appendChild(field("初始密码", initial, true, "密码" + YB.PW_POLICY_HINT));
      stack.appendChild(manualWrap);
    }

    var phone = textInput("af-phone", {
      type: "tel", inputmode: "numeric", maxlength: 20, required: true,
      value: editing ? a.phone : "", placeholder: T.phonePlaceholder
    });
    stack.appendChild(field(T.phoneLabel, phone, true, editing ? T.phoneEditHelp : ""));

    var password = textInput("af-password", {
      type: "password", autocomplete: "new-password",
      placeholder: editing ? "留空表示不修改密码" : T.passwordNewPlaceholder,
      required: !editing
    });
    stack.appendChild(field(T.passwordLabel, password, !editing, null));

    var model = textInput("af-model", {
      maxlength: 50, value: editing ? a.phone_model : "", placeholder: "按易班 App 设备绑定页填写"
    });
    stack.appendChild(field(T.modelLabel, model, false, T.modelHelp));

    var code = textInput("af-code", {
      maxlength: 100,
      placeholder: editing && a.has_phone_code ? "留空表示不修改（已配置）" : "64 位十六进制识别码"
    });
    var clearBtn = YB.el("button", {
      type: "button", class: "btn btn--ghost btn--sm", text: "清除已配置识别码",
      hidden: !(editing && a.has_phone_code)
    });
    stack.appendChild(field(T.codeLabel, code, false, T.codeHelp, clearBtn));
    body.appendChild(stack);

    return {
      body: body, err: err, errText: errText, nodes: {
        name: name, phone: phone, password: password, model: model, code: code,
        clearBtn: clearBtn, email: email, manual: manual, initial: initial,
        manualWrap: manualWrap
      }
    };
  }

  function showError(view, msg) {
    view.errText.textContent = msg || "";
    view.err.hidden = !msg;
  }

  // 绑定用户下拉的固定条目 + 分组头；分组用户列表由 loadAvailableUsers 异步补齐。
  // 可见文本仍是邮箱 local part（脱敏口径不变），完整邮箱只进 value（既有行为）。
  function emailBaseItems() {
    return [
      { v: "", t: "不绑定（管理员自有账号，直接生效）" },
      { v: "__manual__", t: "手填邮箱（未注册用户自动注册）" },
      { group: "已注册用户（无账号）" }
    ];
  }

  function loadAvailableUsers() {
    YB.api("GET", "/api/users").then(function (data) {
      var items = emailBaseItems();
      var list = ((data && data.users) || []).filter(function (u) {
        return (u.account_count || 0) === 0;
      });
      if (!list.length) {
        items.push({ empty: "（暂无）" });   // 不可选空态行，替代原 optgroup 里的凑数 option
      } else {
        list.forEach(function (u) {
          var email = String(u.email || "");
          items.push({ v: email, t: email.split("@")[0] });
        });
      }
      YB.selectField.setOptions("af-email", items);
    }).catch(function () { /* 静默：下拉为空仍可手填（保持初始固定条目 + 空分组） */ });
  }

  // 提交读取走组件 read（隐藏 input 值契约）：__manual__ 时取手填邮箱，其余原样返回
  function emailValue(nodes) {
    var v = YB.selectField.read("af-email");
    return v === "__manual__" ? (nodes.manual ? nodes.manual.value.trim() : "") : v;
  }

  function toggleManual(nodes) {
    if (!nodes.manualWrap) return;
    nodes.manualWrap.hidden = !(nodes.email && YB.selectField.read("af-email") === "__manual__");
  }

  function bindCodeClear(nodes, state) {
    if (!nodes.clearBtn) return;
    nodes.clearBtn.addEventListener("click", function () {
      state.clearCode = !state.clearCode;
      if (state.clearCode) {
        nodes.code.value = "";
        nodes.code.readOnly = true;
        nodes.code.placeholder = "提交后将清除已配置识别码";
        nodes.clearBtn.textContent = "取消清除";
      } else {
        nodes.code.readOnly = false;
        nodes.code.placeholder = "64 位十六进制识别码";
        nodes.clearBtn.textContent = "清除已配置识别码";
      }
    });
  }

  function setBusy(handle, on) {
    var btn = handle.panel.querySelector(".modal-foot .btn--primary");
    if (!btn) return;
    if (on) {
      btn.dataset.label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "提交中…";
    } else {
      btn.disabled = false;
      if (btn.dataset.label) btn.textContent = btn.dataset.label;
    }
  }

  function submit(opts, view, state, snapshot, handle) {
    if (busy) return false;
    showError(view, "");
    var n = view.nodes;
    var editing = opts.index != null;
    var phoneVal = n.phone.value.trim();

    // admin 编辑：详情/快照拿不到时禁止提交——既没有完整号，也没有防并发覆盖的指纹
    if (editing && opts.detail && !snapshot) {
      showError(view, "无法获取账号完整信息，请刷新页面后重试");
      return false;
    }
    if (phoneVal.indexOf("****") !== -1 && !n.phone.dataset.full) {
      showError(view, "无法获取账号完整信息，请刷新页面后重试");
      return false;
    }
    var full = phoneVal.indexOf("****") !== -1 ? n.phone.dataset.full : phoneVal;
    if (!full) { showError(view, "请填写易班手机号"); n.phone.focus(); return false; }
    if (!editing && !n.password.value) { showError(view, "请填写易班密码"); n.password.focus(); return false; }

    var payload = {
      name: n.name.value.trim(),
      phone: full,
      password: n.password.value,
      phone_model: n.model.value.trim(),
      // 已标记清除 → 传 __clear__ 由后端清空；留空表示不修改
      phone_code: state.clearCode ? CODE_CLEAR : n.code.value.trim()
    };
    if (n.email) {
      var email = emailValue(n);
      if (YB.selectField.read("af-email") === "__manual__") {
        if (!email) { showError(view, "请填写邮箱"); return false; }
        if (!YB.passwordPolicyOk(n.initial.value)) {
          showError(view, "初始密码" + YB.PW_POLICY_HINT);
          n.initial.focus();
          return false;
        }
      }
      if (email) payload.email = email;
      payload.initial_password = n.initial.value;
    }
    if (editing && snapshot) payload._snapshot = snapshot;

    busy = true;
    if (opts.lockButton !== false) setBusy(handle, true);
    var req = editing
      ? YB.api("PUT", opts.endpoints.update + opts.index, payload)
      : YB.api("POST", opts.endpoints.create, payload);
    req.then(function (data) {
      busy = false;
      delete n.phone.dataset.full;   // 完整手机号不随已提交的表单节点继续驻留 DOM
      YB.closeModal(handle);
      if (opts.onSaved) opts.onSaved(data);
      YB.toast.success((data && data.msg) || "已保存");
    }).catch(function (e) {
      busy = false;
      if (opts.lockButton !== false) setBusy(handle, false);
      showError(view, (e && e.message) || "保存失败，请稍后再试");
    });
    return false; // 由请求结果决定是否关闭，失败时保持打开
  }

  function open(opts) {
    opts = opts || {};
    var editing = opts.index != null;
    var a = opts.account || {};
    var isUser = opts.variant !== "admin";
    busy = false;
    var state = { clearCode: false };
    var view = build(opts, editing, a);
    var snapshot = null;

    function mount() {
      bindCodeClear(view.nodes, state);
      var submitLabel = opts.submitLabel || (editing ? "保存修改" : (isUser ? "提交账号" : "保存"));
      var handle = YB.openModal({
        title: opts.title || (editing
          ? (isUser ? "编辑我的易班账号" : "编辑账号 #" + (opts.index + 1))
          : (isUser ? "提交我的易班账号" : "添加账号")),
        subtitle: opts.subtitle || (editing
          ? (isUser ? "修改后需重新提交审核。" : "手机号已脱敏，未改动则按原号提交；保存带乐观锁防并发覆盖。")
          : (isUser ? "提交后等待管理员审核，通过即自动签到。" : "不绑定用户则为管理员自有账号（直接生效）；绑定用户需审核。")),
        body: view.body,
        actions: [
          { label: "取消", variant: "ghost" },
          {
            label: submitLabel, variant: "primary",
            onClick: function () { return submit(opts, view, state, snapshot, handle); }
          }
        ]
      });
      if (view.nodes.email) {
        // 绑定用户下拉：openModal 之后才能 mount——select-field 按文档查询 [data-select-field]。
        // change 只由组件在用户选择时派发（程序化 set 不派发），联动展开/收起手填字段。
        view.nodes.email.addEventListener("change", function () { toggleManual(view.nodes); });
        YB.selectField.mount();
        // 接口在途先把固定条目灌进面板，避免用户抢先打开看到空下拉
        YB.selectField.setOptions("af-email", emailBaseItems());
        loadAvailableUsers();
        toggleManual(view.nodes);
      }
    }

    if (editing && opts.detail) {
      YB.api("GET", "/api/accounts/" + opts.index + "/detail").then(function (d) {
        var acc = (d && d.account) || {};
        var fullPhone = String(acc.phone || "");
        // 列表下发的脱敏号是唯一允许回显的值：完整号绝不写进输入框（dataset.full 只暂存，
        // 提交后即清）。列表缺失脱敏号时置空并放弃快照 → 提交走「无法获取」拦截。
        if (!a.phone) {
          view.nodes.phone.dataset.full = "";
          view.nodes.phone.value = "";
          snapshot = null;
          return;
        }
        view.nodes.phone.dataset.full = fullPhone;
        view.nodes.phone.value = a.phone;
        snapshot = JSON.stringify({
          name: acc.name, phone: fullPhone, phone_model: acc.phone_model,
          status: acc.status, deleted: acc.deleted
        });
      }).catch(function () {
        view.nodes.phone.dataset.full = "";
        snapshot = null;
        view.nodes.phone.value = a.phone || "";
      }).then(mount);
    } else {
      mount();
    }
  }

  YB.accountForm = { open: open };
})();
