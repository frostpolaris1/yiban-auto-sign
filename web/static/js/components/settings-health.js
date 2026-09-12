/* 系统设置 · 健康与探针分区（管理端 /settings）。

   挂载到 window.YB.settingsHealth；classic script。
   任意管理员可改（后端 POST /api/settings 对 account_verify / probe_enable /
   probe_time / probe_interval 不限主管理员），故无禁用逻辑。
   改动即保存：每次只提交被改的那一个字段（后端按字段携带写入）。

   服务器连通性探测（POST /api/ping）已删除：管理端总览页 `/` 的运行状态卡已有同款
   能力，设置页不再重复入口。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var saving = false;

  function $(id) { return document.getElementById(id); }
  function tip(text, bad) {
    var el = $("sh-tip");
    if (!el) return;
    el.textContent = text || "";
    el.className = bad ? "set-tip set-bad" : "set-tip";
  }

  // 单字段提交：只发送本次改动的键；失败回滚到服务器值并就地提示。
  function saveOne(field, value, revert) {
    if (saving) { if (revert) revert(); return; }
    saving = true;
    tip("保存中…", false);
    var body = {};
    body[field] = value;
    YB.api("POST", "/api/settings", body).then(function () {
      tip("已保存（将在设定时间后的调度周期自动执行）", false);
    }).catch(function (e) {
      if (revert) revert();
      tip((e && e.message) || "保存失败，请稍后重试", true);
    }).then(function () { saving = false; });
  }

  function bindToggle(id, field) {
    var el = $(id);
    if (!el) return;
    el.addEventListener("change", function () {
      var on = el.checked;
      saveOne(field, on ? 1 : 0, function () { el.checked = !on; });
    });
  }

  function bindTime() {
    var el = $("sh-probe-time");
    if (!el) return;
    el.addEventListener("change", function () {
      var v = String(el.value || "");
      if (!/^\d{2}:\d{2}$/.test(v)) { tip("请选择触发时间", true); return; }
      saveOne("probe_time", v, null);
    });
  }

  function bindInterval() {
    var el = $("sh-probe-interval");
    if (!el) return;
    el.addEventListener("change", function () {
      saveOne("probe_interval", String(el.value || "1"), null);
    });
  }

  function apply(data) {
    var verify = $("sh-verify"), probe = $("sh-probe-enable");
    if (verify) verify.checked = !!data.account_verify;
    if (probe) probe.checked = !!data.probe_enable;
    var time = $("sh-probe-time");
    if (time) time.value = (data.probe_time || "20:00").slice(0, 5);
    var interval = $("sh-probe-interval");
    if (interval) interval.value = data.probe_interval || "1";
  }

  function mount() {
    bindToggle("sh-verify", "account_verify");
    bindToggle("sh-probe-enable", "probe_enable");
    bindTime();
    bindInterval();
  }

  YB.settingsHealth = { mount: mount, apply: apply };
})();
