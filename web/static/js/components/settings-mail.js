/* 系统设置 · 邮件通知卡（管理端 /settings）。
   挂载到 window.YB.settingsMail；classic script。从 settings-notify.js 拆出以控文件长度。

   权限（与后端内联判定 / 高危门禁逐条对齐）：
     · 全局开关、告警收件人、SMTP 列表：仅主管理员；PUT /api/mail-config 的关闭类开关
       与 smtps/admin_to 变更需 confirm_password。
     · 「接收发给我自己的邮件提醒」任意管理员：主管理员走 /api/mail-config {admin_notify}，
       普通管理员走 /api/my-mail-notify。

   脱敏：GET /api/mail-config 的 user/admin_to 与 smtps[].user 已由后端打码；授权码绝不
   回显（pass 输入框恒为空，留空=沿用旧值；user 留空同理）。动态节点一律 YB.el。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ctx = { isMaster: false, mailNotify: true };
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function setTip(text, bad) {
    var el = $("sm-tip");
    if (!el) return;
    el.textContent = text || "";
    el.className = bad ? "set-tip set-bad" : "set-tip";
  }
  function disableAll(root) {
    if (!root) return;
    [].forEach.call(root.querySelectorAll("input,select,button,textarea"), function (n) { n.disabled = true; });
  }
  // 非主管理员：主管理员专属区整块禁用，并把禁用原因 #sm-perm 关联给读屏（group + describedby）
  function applyPerm() {
    if (ctx.isMaster) return;
    var box = $("sm-master-only");
    disableAll(box);
    if (box) {
      box.setAttribute("role", "group");
      box.setAttribute("aria-describedby", "sm-perm");
    }
    var perm = $("sm-perm"); if (perm) perm.hidden = false;
  }
  // 读取失败就地提示 + 重试（不能只置灰，用户无法区分"未配置"与"没读到"）
  function showLoadError(msg) {
    var n = $("sm-tip");
    if (!n) return;
    n.className = "set-tip set-bad";
    n.textContent = (msg || "邮件配置读取失败，请稍后重试") + " ";
    var btn = YB.el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "重试" });
    btn.addEventListener("click", function () {
      n.className = "set-tip";
      n.textContent = "";
      load();
    });
    n.appendChild(btn);
  }
  // 含打码串/占位串的输入一律按空处理（否则按字面落盘会损坏配置）
  function clean(v) {
    var s = String(v || "").trim();
    return (!s || s.indexOf("*") !== -1 || s.charAt(0) === "<") ? "" : s;
  }

  function field(name, label, placeholder, value, type, span, isPass) {
    var wrap = YB.el("div", { class: span || null });
    wrap.appendChild(YB.el("label", { class: "set-label", text: label }));
    var input = YB.el("input", {
      class: "input", type: type, placeholder: placeholder,
      autocomplete: isPass ? "new-password" : "off", "data-f": name
    });
    if (type === "number") { input.min = "1"; input.max = "65535"; }
    if (value !== "" && value != null) input.value = String(value);
    wrap.appendChild(input);
    return wrap;
  }

  function smtpRow(entry, index) {
    var box = YB.el("div", { class: "set-smtp" });
    var head = YB.el("div", { class: "set-smtp-head" });
    head.appendChild(YB.el("span", { class: "set-smtp-name", text: "SMTP " + (index + 1) + (index === 0 ? "（主）" : "（备用）") }));
    var del = YB.el("button", { type: "button", class: "btn btn--ghost btn--sm btn--danger-ghost", text: "删除" });
    del.addEventListener("click", function () { if (ctx.isMaster && box.parentNode) box.parentNode.removeChild(box); });
    head.appendChild(del);
    box.appendChild(head);
    var grid = YB.el("div", { class: "set-smtp-grid" });
    grid.appendChild(field("host", "服务器 host", "smtp.example.com", entry.host || "", "text", "set-span-3", false));
    grid.appendChild(field("port", "端口", "465", entry.port || 465, "number", "", false));
    // user 已由后端打码：只作 placeholder，输入框恒为空（留空=沿用旧值，避免误清）
    grid.appendChild(field("user", "发件账号（留空沿用）", entry.user || "", "", "text", "set-span-2", false));
    grid.appendChild(field("pass", "授权码（永不回显）", entry.has_pass ? "已配置，留空沿用" : "未配置", "", "password", "set-span-3", true));
    box.appendChild(grid);
    return box;
  }

  function renderSmtps(list) {
    var box = $("sm-smtps");
    if (!box) return;
    while (box.firstChild) box.removeChild(box.firstChild);
    list.forEach(function (e, i) { box.appendChild(smtpRow(e, i)); });
    if (!list.length) box.appendChild(YB.el("p", { class: "set-hint", text: "尚未配置发件 SMTP；添加后告警邮件才可送达。" }));
  }

  function load() {
    return YB.api("GET", "/api/mail-config").then(function (data) {
      var status = $("sm-status");
      if (status) {
        status.textContent = data.enabled
          ? "已开启 · 发件 " + (data.user || "未配置") + " · 告警收件 " + (data.admin_to || "未配置")
          : (data.smtps && data.smtps.length
            ? "未开启（已配置发件 SMTP，可由主管理员开启）"
            : "未开启（未配置发件 SMTP）");
      }
      var g = $("sm-global"); if (g) g.checked = !!data.enabled;
      var self = $("sm-self"); if (self) self.checked = ctx.isMaster ? !!data.admin_notify : !!ctx.mailNotify;
      var to = $("sm-to");
      if (to) { to.value = ""; to.placeholder = data.admin_to || "admin@example.com"; }
      renderSmtps(data.smtps || []);
      applyPerm();
    }).catch(function (e) {
      showLoadError((e && e.message) || "邮件配置读取失败，请稍后重试");
    });
  }

  function submitGlobal(next, pw) {
    busy = true;
    var el = $("sm-global");
    var body = { enabled: next };
    if (pw) body.confirm_password = pw;
    YB.api("PUT", "/api/mail-config", body).then(function () {
      setTip(next ? "已开启全局邮件通知" : "已关闭全局邮件通知", false);
      return load();
    }).catch(function (e) {
      if (el) el.checked = !next;
      setTip((e && e.message) || "保存失败，请稍后重试", true);
    }).then(function () { busy = false; });
  }
  function changeGlobal() {
    var el = $("sm-global");
    if (!el || busy) return;
    var next = el.checked;
    // 关闭 = 给全部安全告警拔线（后端高危门禁），先收口令再落盘；开启无口令。
    // change 已把 checked 翻成"关闭"：这里先回滚 UI，取消口令即保持开启态，仅确认成功
    // 后由 load() 按服务端结果落定为关闭 —— 避免"界面显示已关闭但后端仍开着"。
    if (!next) {
      el.checked = !next;
      YB.openConfirmPasswordModal(
        "关闭全局邮件通知？\n关闭后所有安全告警都不再发邮件，且“被关闭”这件事本身也可能没人知道！\n请输入当前管理员密码确认。",
        function (pw) { submitGlobal(next, pw); });
    } else {
      submitGlobal(next, null);
    }
  }

  function submitSelf(next, pw) {
    busy = true;
    var el = $("sm-self");
    var p;
    if (ctx.isMaster) {
      var body = { admin_notify: next };
      if (pw) body.confirm_password = pw;
      p = YB.api("PUT", "/api/mail-config", body);
    } else {
      p = YB.api("PUT", "/api/my-mail-notify", { enabled: next });
    }
    p.then(function () {
      ctx.mailNotify = next;
      setTip(next ? "已开启接收邮件提醒" : "已关闭接收邮件提醒", false);
    }).catch(function (e) {
      if (el) el.checked = !next;
      setTip((e && e.message) || "保存失败，请稍后重试", true);
    }).then(function () { busy = false; });
  }
  function changeSelf() {
    var el = $("sm-self");
    if (!el || busy) return;
    var next = el.checked;
    if (ctx.isMaster && !next) {
      // 同 changeGlobal：先回滚 UI，口令确认成功后再由 load() 落定为关闭
      el.checked = !next;
      YB.openConfirmPasswordModal(
        "关闭主管理员告警邮件接收？\n关闭后 ADMIN_TO 不再收到任何告警邮件（其他管理员收件不受影响）！\n请输入当前管理员密码确认。",
        function (pw) { submitSelf(next, pw); });
      return;
    }
    submitSelf(next, null);
  }

  function saveAdminTo() {
    var el = $("sm-to");
    if (!el || busy) return;
    var val = (el.value || "").trim();
    if (!val) { YB.toast.error("请填写收件人邮箱；如需清空请点「清空」"); return; }
    YB.openConfirmPasswordModal(
      "修改告警收件人？\n告警邮件将改发到新地址，原收件人会被通知。\n请输入当前管理员密码确认。",
      function (pw) {
        busy = true;
        YB.api("PUT", "/api/mail-config", { admin_to: val, confirm_password: pw }).then(function () {
          setTip("已保存告警收件人", false);
          return load();
        }).catch(function (e) {
          setTip((e && e.message) || "保存失败，请稍后重试", true);
        }).then(function () { busy = false; });
      });
  }

  function clearAdminTo() {
    if (busy) return;
    YB.confirmDialog({
      title: "清空告警收件人",
      body: "清空后管理员告警邮件将无人接收（除非另有开启接收的管理员）。确定继续？",
      confirmText: "清空", danger: true
    }).then(function (ok) {
      if (!ok) return;
      YB.openConfirmPasswordModal(
        "再次确认：清空告警收件人？请输入当前管理员密码确认。",
        function (pw) {
          busy = true;
          YB.api("PUT", "/api/mail-config", { admin_to: "", confirm_password: pw }).then(function () {
            setTip("已清空告警收件人", false);
            return load();
          }).catch(function (e) {
            setTip((e && e.message) || "保存失败，请稍后重试", true);
          }).then(function () { busy = false; });
        });
    });
  }

  function collectSmtps() {
    var rows = document.querySelectorAll("#sm-smtps .set-smtp");
    return [].map.call(rows, function (row) {
      return {
        host: clean(row.querySelector('[data-f="host"]').value),
        port: parseInt(row.querySelector('[data-f="port"]').value, 10) || 465,
        user: clean(row.querySelector('[data-f="user"]').value),
        pass: (row.querySelector('[data-f="pass"]').value || "").trim()
      };
    });
  }

  function saveSmtps() {
    if (busy) return;
    var entries = collectSmtps();
    if (entries.length && entries.some(function (e) { return !e.host; })) {
      YB.toast.error("每条 SMTP 都必须填写服务器 host");
      return;
    }
    YB.openConfirmPasswordModal(
      "保存 SMTP 配置：更换/清空邮件通道属敏感操作。\n请输入当前管理员密码确认。",
      function (pw) {
        busy = true;
        YB.api("PUT", "/api/mail-config", { smtps: entries, confirm_password: pw }).then(function () {
          setTip("已保存 SMTP 配置", false);
          return load();
        }).catch(function (e) {
          setTip((e && e.message) || "保存失败，请稍后重试", true);
        }).then(function () { busy = false; });
      });
  }

  function addSmtp() {
    if (!ctx.isMaster) return;
    var box = $("sm-smtps");
    if (!box) return;
    var empty = box.querySelector(".set-hint");
    if (empty) box.removeChild(empty);
    box.appendChild(smtpRow({ host: "", port: 465, user: "", has_pass: false }, box.children.length));
  }

  function mount(options) {
    ctx = {
      isMaster: !!(options && options.isMaster),
      mailNotify: options && options.mailNotify != null ? !!options.mailNotify : true
    };
    var g = $("sm-global"); if (g) g.addEventListener("change", changeGlobal);
    var self = $("sm-self"); if (self) self.addEventListener("change", changeSelf);
    var toSave = $("sm-to-save"); if (toSave) toSave.addEventListener("click", saveAdminTo);
    var toClear = $("sm-to-clear"); if (toClear) toClear.addEventListener("click", clearAdminTo);
    var add = $("sm-add-smtp"); if (add) add.addEventListener("click", addSmtp);
    var saveS = $("sm-save-smtp"); if (saveS) saveS.addEventListener("click", saveSmtps);
    applyPerm();
  }

  YB.settingsMail = { mount: mount, load: load };
})();
