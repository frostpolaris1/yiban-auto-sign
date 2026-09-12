/* 邮件提醒开关（用户端 /user 与 /mine 共用的唯一实现）。
   挂载到 window.YB.myMailNotify；classic script，公开面 mount(opts) → { reload }。

   opts：
     switchSel  checkbox 的 id
     tipSel     就地提示元素 id（aria-live="polite"）
     variant    "user" | "builtin-admin"
     initial    仅 variant === "user" 需要：来自 /api/me 的 mail_notify 布尔

   两条写入路径：
     · user          → PUT /api/my-mail-notify {enabled}
     · builtin-admin → GET /api/mail-config 读 admin_notify 初始化；
                       PUT /api/mail-config {admin_notify}；关闭方向先经
                       YB.openConfirmPasswordModal 取 confirm_password（高危门禁）。
   提示文案一律不含手机号/邮箱等 PII。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  function mount(opts) {
    opts = opts || {};
    var sw = YB.$(opts.switchSel);
    var tip = YB.$(opts.tipSel);
    if (!sw) return { reload: function () {} };

    var isAdmin = opts.variant === "builtin-admin";
    var busy = false;
    var on = isAdmin ? true : !!opts.initial;
    var tipTimer = null;

    function setTip(text) {
      if (!tip) return;
      tip.textContent = text || "";
    }
    // 成功提示短暂保留后清空；重开时清掉旧定时器，避免把新提示提前抹掉
    function setTipTtl(text) {
      setTip(text);
      if (tipTimer) clearTimeout(tipTimer);
      tipTimer = setTimeout(function () { setTip(""); }, 3000);
    }

    /* ---------------- 普通用户：/api/my-mail-notify ---------------- */
    function changeUser() {
      if (busy) return;
      busy = true;
      on = sw.checked;
      setTip("保存中…");
      YB.api("PUT", "/api/my-mail-notify", { enabled: on }).then(function () {
        setTipTtl(on ? "已开启：签到失败时将邮件提醒你" : "已关闭：不再发送签到失败邮件");
      }).catch(function (e) {
        on = !on;                 // 保存失败回滚开关
        sw.checked = on;
        setTip("保存失败：" + ((e && e.message) || "请稍后重试"));
      }).then(function () { busy = false; });
    }

    /* ---------------- 主管理员：/api/mail-config（关闭需口令） ---------------- */
    function submitAdmin(next, pw) {
      busy = true;
      var body = { admin_notify: next };
      if (pw) body.confirm_password = pw;
      setTip("保存中…");
      YB.api("PUT", "/api/mail-config", body).then(function () {
        on = next;
        sw.checked = next;
        setTipTtl(next ? "已开启接收邮件提醒" : "已关闭接收邮件提醒");
      }).catch(function () {
        sw.checked = on;          // 回到最后一次服务端确认值
        setTip("保存失败，请稍后重试");
      }).then(function () { busy = false; });
    }

    function changeAdmin() {
      if (busy) return;
      var next = sw.checked;
      if (!next) {
        // 关闭通道属高危：先回滚 UI，口令确认成功后才落盘；取消则保持开启态
        sw.checked = true;
        YB.openConfirmPasswordModal(
          "关闭主管理员告警邮件接收？关闭后你不再收到任何告警邮件。请输入当前管理员密码确认。",
          function (pw) { submitAdmin(false, pw); }
        );
        return;
      }
      submitAdmin(true, null);
    }

    function loadAdmin() {
      return YB.api("GET", "/api/mail-config").then(function (data) {
        on = !!(data && data.admin_notify);
        sw.checked = on;
      }).catch(function () { /* 读取失败保持开关不动，写入时后端仍会判定 */ });
    }

    /* ---------------- 装配 ---------------- */
    function reload() { return isAdmin ? loadAdmin() : Promise.resolve(); }

    sw.addEventListener("change", isAdmin ? changeAdmin : changeUser);
    if (isAdmin) loadAdmin();
    else sw.checked = on;

    return { reload: reload };
  }

  YB.myMailNotify = { mount: mount };
})();
