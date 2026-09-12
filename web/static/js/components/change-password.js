/* 修改密码弹窗（用户端 /user 与管理端 /mine 共用的唯一实现）。
   挂载到 window.YB.changePassword；classic script，公开面 open({ builtinAdmin, policyOk? })。

   自建弹窗 DOM（不依赖任何模板里的 <template>）：三字段 + 各自显示/隐藏切换按钮。
   口令判定：builtinAdmin 用 12 位三类、否则 10 位至少两类，与后端 web/app.py 同口径；
   open 可选传入 policyOk(password)，供调用页显式承载完整策略（默认按 builtinAdmin 取）。

   改密会轮换会话（后端 users.sid / YIBAN_ADMIN_PW_VERSION 提档），成功提示后跳登录页。
   校验错误显示在弹窗内（role="alert"）：toast 的 z-index 低于模态，用户看不到。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var busy = false;

  function svg(name) {
    return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  }

  function eyeButton(input) {
    var btn = YB.el("button", {
      type: "button", class: "affix-btn",
      "aria-label": "显示密码", "aria-pressed": "false"
    });
    btn.innerHTML = svg("eye");
    btn.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
      btn.setAttribute("aria-pressed", String(show));
      btn.innerHTML = svg(show ? "eye-off" : "eye");
    });
    return btn;
  }

  function pwField(o) {
    var field = YB.el("div", { class: "field" });
    field.appendChild(YB.el("label", { class: "field-label", for: o.id, text: o.label }));
    var affix = YB.el("div", { class: "input-affix" });
    var input = YB.el("input", {
      id: o.id, class: "input", type: "password",
      placeholder: o.placeholder, autocomplete: o.autocomplete, required: true
    });
    if (o.helpId) input.setAttribute("aria-describedby", o.helpId);
    affix.appendChild(input);
    affix.appendChild(eyeButton(input));
    field.appendChild(affix);
    if (o.help) field.appendChild(YB.el("p", { class: "field-help", id: o.helpId, text: o.help }));
    return { field: field, input: input };
  }

  function open(options) {
    options = options || {};
    var isAdmin = !!options.builtinAdmin;
    var hint = isAdmin ? YB.PW_ADMIN_HINT : YB.PW_POLICY_HINT;
    var policyOk = typeof options.policyOk === "function" ? options.policyOk : null;
    busy = false;

    var body = YB.el("div");
    var err = YB.el("div", { class: "alert danger", role: "alert", hidden: true });
    err.appendChild(YB.el("span", { class: "ico", html: svg("circle-alert") }));
    var errText = YB.el("span", { class: "body" });
    err.appendChild(errText);
    body.appendChild(err);

    var stack = YB.el("div", { class: "form-stack" });
    var helpId = "cpw-new-help";
    var oldF = pwField({ id: "cpw-old", label: "当前密码", placeholder: "输入当前密码", autocomplete: "current-password" });
    var newF = pwField({ id: "cpw-new", label: "新密码", placeholder: "新密码", autocomplete: "new-password", helpId: helpId, help: hint });
    var cfmF = pwField({ id: "cpw-confirm", label: "确认新密码", placeholder: "再次输入新密码", autocomplete: "new-password" });
    stack.appendChild(oldF.field);
    stack.appendChild(newF.field);
    stack.appendChild(cfmF.field);
    body.appendChild(stack);

    function reject(msg, input) {
      errText.textContent = msg;
      err.hidden = false;
      if (input) { input.classList.add("is-invalid"); input.focus(); }
      return false;   // 阻止 openModal 自动关闭
    }

    function submit(handle) {
      if (busy) return false;
      errText.textContent = "";
      err.hidden = true;
      var password = newF.input.value;
      // 完整策略判定：长度 + 类别；不得退化为裸长度比较
      var ok = policyOk ? policyOk(password)
        : (isAdmin ? YB.passwordPolicyOkAdmin(password) : YB.passwordPolicyOk(password));
      if (!ok) return reject("新密码" + hint, newF.input);
      if (password !== cfmF.input.value) return reject("两次输入的新密码不一致", cfmF.input);

      busy = true;
      var btns = handle.panel.querySelectorAll(".modal-foot .btn");
      [].forEach.call(btns, function (n) { n.disabled = true; });
      YB.api("POST", "/api/me/password", {
        old_password: oldF.input.value,
        new_password: password,
        confirm_password: password
      }).then(function (data) {
        YB.closeModal(handle);
        YB.toast.success((data && data.msg) || "密码已更新，请重新登录");
        // 改密会轮换会话，必须重新登录
        setTimeout(function () { location.href = YB.BASE + "/login"; }, 1200);
      }).catch(function (e) {
        busy = false;
        [].forEach.call(btns, function (n) { n.disabled = false; });
        reject((e && e.message) || "修改失败，请稍后再试", null);
      });
      return false;   // 由请求结果决定是否关闭
    }

    return YB.openModal({
      title: isAdmin ? "修改主管理员密码" : "修改密码",
      subtitle: "账号（注册邮箱）不可修改，改后下次登录使用新密码。",
      body: body,
      actions: [
        { label: "取消", variant: "ghost" },
        { label: "保存新密码", variant: "primary", onClick: function (handle) { return submit(handle); } }
      ]
    });
  }

  YB.changePassword = { open: open };
})();
