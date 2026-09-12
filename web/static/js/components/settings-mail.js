/* 系统设置 · 邮件段（管理端 /settings 的「通知通道」分区）。

   挂载到 window.YB.settingsMail；classic script。消息推送段在 settings-notify.js，
   两段同处 #set-notify 一张卡；非主管理员的整卡禁用由 settings-notify 统一处理，
   本组件不再重复判定（动作里仍做 isMaster 早退，UI 不是安全边界）。

   权限（与后端高危门禁逐条对齐）：
     · 全局开关、告警收件人、SMTP 列表：仅主管理员；PUT /api/mail-config 的关闭类
       开关与 smtps/admin_to 变更需 confirm_password。
     · 「接收发给我自己的邮件提醒」是**个人域**，已迁到 /mine，本页不再有。

   脱敏：GET /api/mail-config 的 admin_to 与 smtps[].user 已由后端打码；授权码绝不
   回显（pass 输入框恒为空，留空=沿用旧值；user 留空同理，打码值只作 placeholder）。
   SMTP 列表用模板 .data-table 行内编辑。动态节点一律 YB.el。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ctx = { isMaster: false };
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function tbody() { return document.querySelector("#sm-smtps tbody"); }
  function setTip(text, bad) {
    var el = $("sm-tip");
    if (!el) return;
    el.textContent = text || "";
    el.className = bad ? "set-tip set-bad" : "set-tip";
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

  // 非敏感字段（host / port）回填当前值，便于在原配置上修改
  function cellInput(name, type, placeholder, ariaLabel, value) {
    var input = YB.el("input", {
      class: "input", type: type, placeholder: placeholder,
      autocomplete: "off", "data-f": name, "aria-label": ariaLabel
    });
    if (type === "number") { input.min = "1"; input.max = "65535"; }
    if (value !== "" && value != null) input.value = String(value);
    return input;
  }

  // 脱敏字段：后端下发的 user 已打码、授权码永不回显 —— **绝不回填 value**，只作 placeholder
  // （回填会让保存把打码串当真实值落盘，损坏配置）。
  function maskedCellInput(name, type, placeholder, ariaLabel) {
    return YB.el("input", {
      class: "input", type: type, placeholder: placeholder,
      autocomplete: type === "password" ? "new-password" : "off",
      "data-f": name, "aria-label": ariaLabel
    });
  }

  function smtpRow(entry, index) {
    var tr = YB.el("tr");
    var tdHost = YB.el("td");
    tdHost.appendChild(cellInput("host", "text", "smtp.example.com", "SMTP " + (index + 1) + " 服务器 host", entry.host || ""));
    var tdPort = YB.el("td");
    tdPort.appendChild(cellInput("port", "number", "465", "SMTP " + (index + 1) + " 端口", entry.port || 465));
    var tdUser = YB.el("td");
    tdUser.appendChild(maskedCellInput("user", "text", entry.user || "留空沿用", "SMTP " + (index + 1) + " 发件账号"));
    var tdPass = YB.el("td");
    tdPass.appendChild(maskedCellInput("pass", "password", entry.has_pass ? "已配置，留空沿用" : "未配置", "SMTP " + (index + 1) + " 授权码"));
    var tdOps = YB.el("td");
    var del = YB.el("button", { type: "button", class: "btn btn--ghost btn--sm btn--danger-ghost", text: "删除" });
    del.setAttribute("aria-label", "删除 SMTP " + (index + 1));
    del.addEventListener("click", function () {
      if (!ctx.isMaster) return;
      if (tr.parentNode) tr.parentNode.removeChild(tr);
      renumber();
    });
    tdOps.appendChild(del);
    tr.appendChild(tdHost);
    tr.appendChild(tdPort);
    tr.appendChild(tdUser);
    tr.appendChild(tdPass);
    tr.appendChild(tdOps);
    return tr;
  }

  // 删除后重排行内 aria-label（保持读屏序号与服务端顺序一致）
  function renumber() {
    var rows = tbody() ? tbody().querySelectorAll("tr") : [];
    [].forEach.call(rows, function (tr, i) {
      [].forEach.call(tr.querySelectorAll("[data-f]"), function (inp) {
        var map = { host: "服务器 host", port: "端口", user: "发件账号", pass: "授权码" };
        inp.setAttribute("aria-label", "SMTP " + (i + 1) + " " + (map[inp.getAttribute("data-f")] || "字段"));
      });
    });
  }

  function emptyRow() {
    var tr = YB.el("tr", { class: "sm-empty-row" });
    var td = YB.el("td", { colspan: "5" });
    td.appendChild(YB.el("p", { class: "field-help", text: "尚未配置发件 SMTP；添加后告警邮件才可送达。" }));
    tr.appendChild(td);
    return tr;
  }

  function renderSmtps(list) {
    var body = tbody();
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);
    if (!list.length) { body.appendChild(emptyRow()); return; }
    list.forEach(function (e, i) { body.appendChild(smtpRow(e, i)); });
  }

  function load() {
    if (!ctx.isMaster) return Promise.resolve();   // 非主管理员不拉（整卡已禁用，避免渲染出"看似可编辑"的行）
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
      var to = $("sm-to");
      if (to) { to.value = ""; to.placeholder = data.admin_to || "admin@example.com"; }
      renderSmtps(data.smtps || []);
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
    if (!el || busy || !ctx.isMaster) return;
    var next = el.checked;
    // 关闭 = 给全部安全告警拔线（后端高危门禁），先收口令再落盘；开启无口令。
    // change 已把 checked 翻成"关闭"：先回滚 UI，取消口令即保持开启态，仅确认成功
    // 后由 load() 按服务端结果落定为关闭 —— 避免"界面显示已关闭但后端仍开着"。
    if (!next) {
      el.checked = true;
      YB.openConfirmPasswordModal(
        "关闭全局邮件通知？\n关闭后所有安全告警都不再发邮件，且“被关闭”这件事本身也可能没人知道！\n请输入当前管理员密码确认。",
        function (pw) { submitGlobal(next, pw); });
    } else {
      submitGlobal(next, null);
    }
  }

  function saveAdminTo() {
    var el = $("sm-to");
    if (!el || busy || !ctx.isMaster) return;
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
    if (busy || !ctx.isMaster) return;
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
    var body = tbody();
    if (!body) return [];
    return [].map.call(body.querySelectorAll("tr"), function (row) {
      var host = row.querySelector('[data-f="host"]');
      if (!host) return null;
      return {
        host: clean(host.value),
        port: parseInt(row.querySelector('[data-f="port"]').value, 10) || 465,
        user: clean(row.querySelector('[data-f="user"]').value),
        pass: (row.querySelector('[data-f="pass"]').value || "").trim()
      };
    }).filter(Boolean);
  }

  function saveSmtps() {
    if (busy || !ctx.isMaster) return;
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
    var body = tbody();
    if (!body) return;
    var empty = body.querySelector(".sm-empty-row");
    if (empty) body.removeChild(empty);
    body.appendChild(smtpRow({ host: "", port: 465, user: "", has_pass: false }, body.children.length));
  }

  function mount(options) {
    ctx = { isMaster: !!(options && options.isMaster) };
    var g = $("sm-global"); if (g) g.addEventListener("change", changeGlobal);
    var toSave = $("sm-to-save"); if (toSave) toSave.addEventListener("click", saveAdminTo);
    var toClear = $("sm-to-clear"); if (toClear) toClear.addEventListener("click", clearAdminTo);
    var add = $("sm-add-smtp"); if (add) add.addEventListener("click", addSmtp);
    var saveS = $("sm-save-smtp"); if (saveS) saveS.addEventListener("click", saveSmtps);
  }

  YB.settingsMail = { mount: mount, load: load };
})();
