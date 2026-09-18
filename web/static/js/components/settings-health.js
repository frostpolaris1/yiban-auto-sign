/* 系统设置 · 健康与探针分区（管理端 /work/settings）。

   挂载到 window.YB.settingsHealth；classic script。
   探针/账号验证会对**全站账号**做真实登录（与签到同一风控面），M12 起收归主管理员，
   档位单源是后端 `web/app.py` 的 `MASTER_ONLY_KEYS`（本文件不再抄第二份键名清单，
   字段名保持 `body.<键> = …` 直写形态供对拍测试读取）。前端禁用控件并挂权限说明
   （#sh-perm，读屏可及），保存一律先过口令框（A 档值变了必须当次 confirm_password）。

   保存语义（与全页统一）：改动只标脏（脏徽标 + 保存按钮出现），点「保存探针设置」
   才提交，只发送真正变化的字段；此前这里是"改动即保存"，与同页其它卡片不一致
   （用户实测判"自动保存和手动保存操作不统一"）。

   对外面：apply(data) 回填、save() → Promise<boolean>（false = 失败，页面据此不跳转）、
   isDirty()。

   服务器连通性探测（POST /api/ping）已删除：管理端总览页 `/` 的运行状态卡已有同款
   能力，设置页不再重复入口。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var snap = null;
  var dirty = false;
  var saving = false;
  var ctx = { isMaster: false };

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  function tip(text, bad) { YB.setTip("sh-tip", text, bad); }
  function markDirty() {
    if (dirty) return;
    dirty = true;
    setHidden($("sh-save"), false);
    setHidden($("sh-dirty"), false);
  }
  function clearDirty() {
    dirty = false;
    setHidden($("sh-save"), true);
    setHidden($("sh-dirty"), true);
  }

  // 健康/探针按权限启用/禁用：禁用时把原因 #sh-perm 与控件程序化关联（读屏可及）。
  function applyPerm() {
    var disabled = !ctx.isMaster;
    ["sh-verify", "sh-probe-enable"].forEach(function (id) {
      var n = $(id);
      if (!n) return;
      n.disabled = !!disabled;
      if (disabled) n.setAttribute("aria-describedby", "sh-perm");
      else n.removeAttribute("aria-describedby");
    });
    if (YB.timeField && YB.timeField.setDisabled) YB.timeField.setDisabled("sh-probe-time", disabled);
    if (YB.selectField && YB.selectField.setDisabled) YB.selectField.setDisabled("sh-probe-interval", disabled);
    var btn = $("sh-save");
    if (btn) btn.disabled = !!disabled;
    setHidden($("sh-save"), disabled || !dirty);
    setHidden($("sh-dirty"), disabled || !dirty);
    setHidden($("sh-perm"), !disabled);
  }

  function domSnapshot() {
    return {
      verify: $("sh-verify") && $("sh-verify").checked ? 1 : 0,
      probe: $("sh-probe-enable") && $("sh-probe-enable").checked ? 1 : 0,
      time: (($("sh-probe-time") || {}).value || "20:00").slice(0, 5),
      interval: ($("sh-probe-interval") || {}).value || "1"
    };
  }

  function collect() {
    var now = domSnapshot();
    var body = {};
    if (now.verify !== snap.verify) body.account_verify = now.verify;
    if (now.probe !== snap.probe) body.probe_enable = now.probe;
    if (now.time !== snap.time) body.probe_time = now.time;
    if (now.interval !== snap.interval) body.probe_interval = now.interval;
    return body;
  }

  function submit(body, fromPw) {
    saving = true;
    tip("保存中…", false);
    setHidden($("sh-save"), true);
    return YB.api("POST", "/api/settings", body).then(function (data) {
      snap = domSnapshot();
      clearDirty();
      tip((data && data.msg) || "探针设置已保存（将在设定时间后的调度周期自动执行）", false);
      return true;
    }, function (e) {
      // 从口令框发起：错误原样抛回去，让它显示在框内并保留输入以便改口令重试
      if (fromPw) throw e;
      tip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      saving = false;
      setHidden($("sh-save"), !dirty);
      return ok;
    }, function (e) {                    // 失败也要复位 saving，否则保存按钮永久卡住
      saving = false;
      setHidden($("sh-save"), !dirty);
      throw e;
    });
  }

  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 提交失败。
  function save() {
    if (saving || !ctx.isMaster) return Promise.resolve(false);
    var body = collect();
    if (!Object.keys(body).length) {
      clearDirty();
      YB.toast.info("没有需要保存的改动");
      return Promise.resolve(true);
    }
    // A 档：值真的变了就必须当次口令（后端 _high_risk_gate），不收口令直接 403
    return new Promise(function (resolve) {
      YB.openConfirmPasswordModal(
        "探针与账号验证会让服务器对全站账号发起真实易班登录（与签到同一风控面）。请输入当前管理员密码确认。",
        function (pw) { body.confirm_password = pw; submit(body, true).then(resolve, function () { resolve(false); }); },
        function () { resolve(false); });      // 取消口令 = 本次不保存
    });
  }

  function apply(data) {
    snap = {
      verify: data.account_verify ? 1 : 0,
      probe: data.probe_enable ? 1 : 0,
      time: String(data.probe_time || "20:00").slice(0, 5),
      interval: String(data.probe_interval || "1")
    };
    var verify = $("sh-verify"), probe = $("sh-probe-enable");
    if (verify) verify.checked = !!snap.verify;
    if (probe) probe.checked = !!snap.probe;
    YB.timeField.set("sh-probe-time", snap.time);
    YB.selectField.set("sh-probe-interval", snap.interval);
    clearDirty();
    applyPerm();
    tip("", false);
  }

  function mount() {
    ["sh-verify", "sh-probe-enable"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", markDirty);
    });
    var time = $("sh-probe-time");
    if (time) time.addEventListener("change", markDirty);
    var interval = $("sh-probe-interval");
    if (interval) interval.addEventListener("change", markDirty);
    var btn = $("sh-save");
    if (btn) btn.addEventListener("click", function () { save(); });
    ctx = { isMaster: !!(arguments[0] && arguments[0].isMaster) };
    applyPerm();
  }

  YB.settingsHealth = { mount: mount, apply: apply, save: save, isDirty: function () { return dirty; } };
})();
