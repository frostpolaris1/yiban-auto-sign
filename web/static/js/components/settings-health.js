/* 系统设置 · 健康与探针分区（管理端 /settings）。

   挂载到 window.YB.settingsHealth；classic script。
   任意管理员可改（后端 POST /api/settings 对 account_verify / probe_enable /
   probe_time / probe_interval 不限主管理员），故无禁用逻辑。

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

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  function tip(text, bad) {
    var el = $("sh-tip");
    if (!el) return;
    el.textContent = text || "";
    el.className = bad ? "set-tip set-bad" : "set-tip";
  }
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

  function submit(body) {
    saving = true;
    tip("保存中…", false);
    setHidden($("sh-save"), true);
    return YB.api("POST", "/api/settings", body).then(function (data) {
      snap = domSnapshot();
      clearDirty();
      tip((data && data.msg) || "探针设置已保存（将在设定时间后的调度周期自动执行）", false);
      return true;
    }, function (e) {
      tip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      saving = false;
      setHidden($("sh-save"), !dirty);
      return ok;
    });
  }

  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 提交失败。
  function save() {
    if (saving) return Promise.resolve(false);
    var body = collect();
    if (!Object.keys(body).length) {
      clearDirty();
      YB.toast.info("没有需要保存的改动");
      return Promise.resolve(true);
    }
    return submit(body);
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
  }

  YB.settingsHealth = { mount: mount, apply: apply, save: save, isDirty: function () { return dirty; } };
})();
