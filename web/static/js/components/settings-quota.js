/* 系统设置 · 容量配额分区（管理端 /settings）。

   挂载到 window.YB.settingsQuota；classic script。仅主管理员可改
   （后端 POST /api/settings 的 403 列表含 max_users / max_accounts）。
   非主管理员：控件禁用 + 就地说明（可见即理解权限）。

   只保留上限编辑：只读用量三分类与容量估算已删除 —— /accounts 的容量卡已给出
   同口径展示，设置页不再重复。保存走 confirm_password 高危门禁（不合适的上限会
   影响新增注册/账号），成功后回调页面刷新容量状态。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var ctx = { isMaster: false, onSaved: null };
  var state = { capUsers: 0, capAccounts: 0 };
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function setTip(text, bad) {
    var el = $("set-cap-tip");
    if (!el) return;
    el.textContent = text || "";
    el.className = bad ? "set-tip set-bad" : "set-tip";
  }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }

  // 容量上限按权限启用/禁用；禁用时把原因 #set-cap-perm 与控件做程序化关联（读屏可及）。
  function applyPerm() {
    var disabled = !ctx.isMaster;
    ["set-max-users", "set-max-accounts", "set-cap-save"].forEach(function (id) {
      var n = $(id);
      if (!n) return;
      n.disabled = !!disabled;
      if (disabled) n.setAttribute("aria-describedby", "set-cap-perm");
      else n.removeAttribute("aria-describedby");
    });
    setHidden($("set-cap-perm"), !disabled);
  }

  function apply(data) {
    var cap = (data && data.capacity) || {};
    state.capUsers = Number(cap.users_max) || 0;
    state.capAccounts = Number(cap.accounts_max) || 0;
    var u = $("set-max-users"), a = $("set-max-accounts");
    if (u) u.value = String(state.capUsers);
    if (a) a.value = String(state.capAccounts);
    setTip("", false);
    applyPerm();
  }

  function save() {
    if (busy || !ctx.isMaster) return;
    var u = parseInt(($("set-max-users") || {}).value, 10);
    var a = parseInt(($("set-max-accounts") || {}).value, 10);
    if (isNaN(u)) u = state.capUsers;
    if (isNaN(a)) a = state.capAccounts;
    var body = {};
    if (u !== state.capUsers) body.max_users = u;
    if (a !== state.capAccounts) body.max_accounts = a;
    if (!Object.keys(body).length) { setTip("没有需要保存的改动", false); return; }
    YB.openConfirmPasswordModal(
      "调整容量上限：不合适的设置可能影响新增注册/账号，是否继续？\n请输入当前管理员密码确认。",
      function (pw) {
        body.confirm_password = pw;
        busy = true;
        setTip("保存中…", false);
        YB.api("POST", "/api/settings", body).then(function (data) {
          setTip((data && data.msg) || "容量上限已保存", false);
          if (ctx.onSaved) ctx.onSaved(data);
        }).catch(function (e) {
          setTip((e && e.message) || "保存失败，请稍后重试", true);
        }).then(function () { busy = false; });
      });
  }

  function mount(options) {
    ctx = {
      isMaster: !!(options && options.isMaster),
      onSaved: options && typeof options.onSaved === "function" ? options.onSaved : null
    };
    var btn = $("set-cap-save");
    if (btn) btn.addEventListener("click", save);
    applyPerm();
  }

  YB.settingsQuota = { mount: mount, apply: apply };
})();
