/* 系统设置 · 邮件段（管理端 /settings 的「通知通道」分区）。

   挂载到 window.YB.settingsMail；classic script。消息推送段在 settings-notify.js，
   两段各占一张卡；非主管理员的整卡禁用由 settings-notify 统一处理，
   本组件不再重复判定（动作里仍做 isMaster 早退，UI 不是安全边界）。

   权限（与后端 PUT /api/mail-config 的高危门禁逐条对齐）：
     · 全局开关、告警收件人、SMTP 列表：仅主管理员；
     · 关闭类开关、admin_to 与 smtps 变更需 confirm_password（后端一次请求只验一次）。
     · 「接收发给我自己的邮件提醒」是**个人域**，已迁到 /mine，本页不再有。

   保存语义（与全页统一）：全局开关、收件人、SMTP 列表合并为**一个**「保存邮件配置」，
   只提交相对快照真正变化的键；纯"开启"不带口令（后端同口径：不给正常成功路径加摩擦），
   关闭/改地址/改 SMTP 才收管理员口令。「清空收件人」是动作（不属表单值），单独确认。
   对外面：mount/load/apply(load 同义)、save() → Promise<boolean>、isDirty()。

   脱敏：GET /api/mail-config 的 admin_to 与 smtps[].user 已由后端打码；授权码绝不
   回显（pass 输入框恒为空，留空=沿用旧值；user 留空同理，打码值只作 placeholder）。
   SMTP 列表用模板 .data-table 行内编辑。动态节点一律 YB.el。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ctx = { isMaster: false };
  var snap = { enabled: false, hasTo: false };
  var busy = false;
  var dirty = false;
  var tableDirty = false;

  function $(id) { return document.getElementById(id); }
  function tbody() { return document.querySelector("#sm-smtps tbody"); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  function setTip(text, bad) {
    var el = $("sm-tip");
    if (!el) return;
    el.textContent = text || "";
    el.className = bad ? "set-tip set-bad" : "set-tip";
  }
  function markDirty() {
    if (dirty) return;
    dirty = true;
    setHidden($("sm-save"), false);
    setHidden($("sm-dirty"), false);
  }
  function clearDirty() {
    dirty = false;
    tableDirty = false;
    setHidden($("sm-save"), true);
    setHidden($("sm-dirty"), true);
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
    var tdPort = YB.el("td", { class: "num" });
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
      tableDirty = true;
      markDirty();
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
      // admin_to 是打码后的展示串：未配置时后端给 "<未配置>" 哨兵，已配置则形如 abc***@x.com。
      // 故"是否已配置"只排除哨兵与空串 —— 用 clean() 会把打码真值也当成空（那是给输入框用的口径）。
      var toShown = String((data && data.admin_to) || "");
      snap = {
        enabled: !!data.enabled,
        hasTo: !!toShown && toShown.charAt(0) !== "<"
      };
      var status = $("sm-status");
      if (status) {
        status.textContent = data.enabled
          ? "已开启 · 发件 " + (data.user || "未配置") + " · 告警收件 " + (data.admin_to || "未配置")
          : (data.smtps && data.smtps.length
            ? "未开启（已配置发件 SMTP，可由主管理员开启）"
            : "未开启（未配置发件 SMTP）");
      }
      var g = $("sm-global"); if (g) g.checked = snap.enabled;
      var to = $("sm-to");
      if (to) { to.value = ""; to.placeholder = data.admin_to || "admin@example.com"; }
      setHidden($("sm-to-clear"), !snap.hasTo);
      renderSmtps(data.smtps || []);
      clearDirty();
      setTip("", false);
    }).catch(function (e) {
      showLoadError((e && e.message) || "邮件配置读取失败，请稍后重试");
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

  // 只提交真正变化的键：开关单独变时不重写 SMTP 列表（避免把未改动的行也落盘一遍）
  function collect() {
    var body = {};
    var g = $("sm-global");
    if (g && !!g.checked !== snap.enabled) body.enabled = !!g.checked;
    var to = $("sm-to");
    var toVal = to ? String(to.value || "").trim() : "";
    if (toVal) body.admin_to = toVal;
    if (tableDirty) body.smtps = collectSmtps();
    return body;
  }

  function submit(body) {
    var needPw = Object.prototype.hasOwnProperty.call(body, "admin_to") ||
      Object.prototype.hasOwnProperty.call(body, "smtps") || body.enabled === false;
    if (!needPw) return write(body);
    return new Promise(function (resolve) {
      YB.openConfirmPasswordModal(
        "保存邮件配置：关闭全局通知、修改告警收件人或更换 SMTP 通道属敏感操作。\n请输入当前管理员密码确认。",
        function (pw) { write(body, pw).then(resolve); },
        function () { resolve(false); });     // 取消口令 = 本次不保存
    });
  }

  function write(body, pw) {
    if (pw) body.confirm_password = pw;
    busy = true;
    var btn = $("sm-save"); if (btn) btn.disabled = true;
    setTip("保存中…", false);
    return YB.api("PUT", "/api/mail-config", body).then(function () {
      var to = $("sm-to"); if (to) to.value = "";
      setTip("邮件配置已保存", false);
      return load().then(function () { return true; });
    }, function (e) {
      setTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      busy = false;
      if (btn && ctx.isMaster) btn.disabled = false;
      return ok;
    });
  }

  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 取消或失败。
  function save() {
    if (busy || !ctx.isMaster) return Promise.resolve(false);
    var body = collect();
    if (!Object.keys(body).length) {
      clearDirty();
      YB.toast.info("没有需要保存的改动");
      return Promise.resolve(true);
    }
    if (Object.prototype.hasOwnProperty.call(body, "smtps")) {
      var entries = body.smtps;
      if (entries.some(function (e) { return !e.host; })) {
        YB.toast.error("每条 SMTP 都必须填写服务器 host");
        return Promise.resolve(false);
      }
    }
    return submit(body);
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

  function addSmtp() {
    if (!ctx.isMaster) return;
    var body = tbody();
    if (!body) return;
    var empty = body.querySelector(".sm-empty-row");
    if (empty) body.removeChild(empty);
    body.appendChild(smtpRow({ host: "", port: 465, user: "", has_pass: false }, body.children.length));
    tableDirty = true;
    markDirty();
  }

  function mount(options) {
    ctx = { isMaster: !!(options && options.isMaster) };
    var g = $("sm-global"); if (g) g.addEventListener("change", markDirty);
    var to = $("sm-to"); if (to) to.addEventListener("input", markDirty);
    var body = tbody();
    if (body) body.addEventListener("input", function () { tableDirty = true; markDirty(); });
    var saveBtn = $("sm-save"); if (saveBtn) saveBtn.addEventListener("click", function () { save(); });
    var toClear = $("sm-to-clear"); if (toClear) toClear.addEventListener("click", clearAdminTo);
    var add = $("sm-add-smtp"); if (add) add.addEventListener("click", addSmtp);
  }

  YB.settingsMail = {
    mount: mount, load: load, save: save,
    isDirty: function () { return dirty; }
  };
})();
