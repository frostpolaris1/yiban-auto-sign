/* 系统设置 · 容量配额分区（管理端 /settings）。

   挂载到 window.YB.settingsQuota；classic script。仅主管理员可改
   （后端 POST /api/settings 的 403 列表含 max_users / max_accounts）。
   非主管理员：控件禁用 + 就地说明（可见即理解权限）。

   只保留上限编辑：只读用量三分类与容量估算已删除 —— /accounts 的容量卡已给出
   同口径展示，设置页不再重复。

   保存语义（与全页统一）：改动只标脏（脏徽标 + 保存按钮出现），点「保存容量上限」
   才提交；保存走 confirm_password 高危门禁（不合适的上限会影响新增注册/账号），
   成功后回调页面刷新容量状态。
   对外面：apply(data) 回填、save() → Promise<boolean>（false = 取消或失败）。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ctx = { isMaster: false, onSaved: null };
  var snap = { users: 0, accounts: 0 };
  var dirty = false;
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  function setTip(text, bad) { YB.setTip("set-cap-tip", text, bad); }
  function value(id, fallback) {
    var n = parseInt(($(id) || {}).value, 10);
    return isNaN(n) ? fallback : n;
  }
  function markDirty() {
    if (dirty) return;
    dirty = true;
    setHidden($("set-cap-save"), false);
    setHidden($("set-cap-dirty"), false);
  }
  function clearDirty() {
    dirty = false;
    setHidden($("set-cap-save"), true);
    setHidden($("set-cap-dirty"), true);
  }

  // 容量上限按权限启用/禁用；禁用时把原因 #set-cap-perm 与控件做程序化关联（读屏可及）。
  function applyPerm() {
    var disabled = !ctx.isMaster;
    ["set-max-users", "set-max-accounts"].forEach(function (id) {
      var n = $(id);
      if (!n) return;
      n.disabled = !!disabled;
      if (disabled) n.setAttribute("aria-describedby", "set-cap-perm");
      else n.removeAttribute("aria-describedby");
    });
    var btn = $("set-cap-save");
    if (btn) btn.disabled = !!disabled;
    setHidden(btn, disabled || !dirty);
    setHidden($("set-cap-dirty"), disabled || !dirty);
    setHidden($("set-cap-perm"), !disabled);
  }

  function collect() {
    var body = {};
    var u = value("set-max-users", snap.users);
    var a = value("set-max-accounts", snap.accounts);
    if (u !== snap.users) body.max_users = u;
    if (a !== snap.accounts) body.max_accounts = a;
    return body;
  }

  function submit(body) {
    return new Promise(function (resolve) {
      YB.openConfirmPasswordModal(
        "调整容量上限：不合适的设置可能影响新增注册/账号，是否继续？\n请输入当前管理员密码确认。",
        function (pw) {
          body.confirm_password = pw;
          busy = true;
          setTip("保存中…", false);
          YB.api("POST", "/api/settings", body).then(function (data) {
            snap = { users: value("set-max-users", snap.users), accounts: value("set-max-accounts", snap.accounts) };
            clearDirty();
            setTip((data && data.msg) || "容量上限已保存", false);
            if (ctx.onSaved) ctx.onSaved(data);
            resolve(true);
          }, function (e) {
            setTip((e && e.message) || "保存失败，请稍后重试", true);
            resolve(false);
          }).then(function () {
            busy = false;
            applyPerm();
          });
        },
        function () { resolve(false); });     // 取消口令 = 本次不保存
    });
  }

  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 取消或失败。
  function save() {
    if (busy || !ctx.isMaster) return Promise.resolve(false);
    var body = collect();
    if (!Object.keys(body).length) {
      clearDirty();
      setTip("没有需要保存的改动", false);
      return Promise.resolve(true);
    }
    return submit(body);
  }

  function apply(data) {
    var cap = (data && data.capacity) || {};
    snap = { users: Number(cap.users_max) || 0, accounts: Number(cap.accounts_max) || 0 };
    var u = $("set-max-users"), a = $("set-max-accounts");
    if (u) u.value = String(snap.users);
    if (a) a.value = String(snap.accounts);
    clearDirty();
    setTip("", false);
    applyPerm();
  }

  function mount(options) {
    ctx = {
      isMaster: !!(options && options.isMaster),
      onSaved: options && typeof options.onSaved === "function" ? options.onSaved : null
    };
    var btn = $("set-cap-save");
    if (btn) btn.addEventListener("click", function () { save(); });
    ["set-max-users", "set-max-accounts"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", function () {
        var cur = { users: value("set-max-users", snap.users), accounts: value("set-max-accounts", snap.accounts) };
        if (cur.users === snap.users && cur.accounts === snap.accounts) clearDirty();
        else markDirty();
      });
    });
    applyPerm();
  }

  YB.settingsQuota = { mount: mount, apply: apply, save: save, isDirty: function () { return dirty; } };
})();
