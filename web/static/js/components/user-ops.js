/* 用户管理页（管理端 /users）全部写操作的统一入口。
   挂载到 window.YB.userOps；classic script，YB.userOps.create(ctx) 返回方法集。

   把「确认 → 口令二次鉴权 → 调接口 → 成功提示 → 刷新」这条固定链路收在一处。
   页面只提供 ctx：
     ctx.busy(on)     置/复位忙碌
     ctx.refresh()    写操作成功后重拉两个列表并重渲染
     ctx.resolve(uid) 由内部 uid 回查用户记录（含完整邮箱）——完整邮箱只在请求体
                      或既有契约的 URL path 里出现，绝不写入 DOM

   与后端门禁逐条对齐（web/app.py）：
     · role        仅主管理员 + _high_risk_gate（必须带 confirm_password）
     · reset       重置口令入口由页面模态收新密码（走完整策略），此处做 _high_risk_gate；
                   目标为注册管理员时后端再限主管理员
     · delete      删用户不可逆：走 YB.dangerousSubmit，由后端响应 reason 决定要口令
                   （password_required）还是倒计时确认（delay_ack_required）
     · purge       已注销用户立即清除：仅主管理员；不可逆，同 delete 走 dangerousSubmit
     · batch       reset_password（需 password 过策略 + confirm_password）保持口令框；
                   delete 不可逆，走 dangerousSubmit；角色变更不支持批量；emails 单次 <= 10
   档位只存在于后端，本组件不判断档位、只按 reason 分流。
   批量超出上限在前端先拦（提示后不发请求），避免落到后端 400。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var LIMIT = 10; // BATCH_OP_LIMIT，与后端 /api/users/batch、/api/users/deleted/purge 一致

  function create(ctx) {
    function fail(e) {
      // 用户取消弹窗（dangerousSubmit 以 canceled 标记拒绝）：不是失败，不提示
      if (e && e.canceled) return;
      YB.toast.error((e && e.message) || "操作失败，请稍后再试");
    }
    // 单目标端点的成功 msg 含**完整邮箱**（role / password / delete 三处），一律不使用后端
    // msg 上屏，只弹本地无 PII 文案——否则完整邮箱经 toast 进入 DOM。batch / purge 的 msg
    // 只含数量，才允许使用后端 msg（useServerMsg=true）。
    function successMsg(data, fallback, useServerMsg) {
      if (!fallback) return "";
      if (!useServerMsg) return fallback;
      var m = data ? data["msg"] : "";
      return m || fallback;
    }
    // 统一链路：置忙 → 请求 → 成功提示 + 刷新 → 复位。fallback 为空表示不弹成功提示。
    function run(promise, fallback, useServerMsg) {
      ctx.busy(true);
      return promise.then(function (data) {
        var msg = successMsg(data, fallback, useServerMsg);
        if (msg) YB.toast.success(msg);
        ctx.refresh();
      }).catch(fail).then(function () { ctx.busy(false); });
    }
    function post(path, body) { return YB.api("POST", path, body); }
    function emailOf(uid) {
      var rec = ctx.resolve(uid);
      return rec && rec.email ? rec.email : "";
    }
    function path(email, tail) { return "/api/users/" + encodeURIComponent(email) + tail; }

    // 角色变更：仅主管理员（后端 403 兜底）；确认 → 口令鉴权 → 提交
    function role(uid, newRole) {
      var email = emailOf(uid);
      if (!email) return;
      var action = newRole === "admin" ? "设为管理员" : "取消管理员";
      YB.confirmDialog({
        title: action,
        body: "确定将 " + YB.maskEmail(email) + " " + action + "吗？该操作会立即改变其权限。",
        confirmText: action, danger: newRole !== "admin"
      }).then(function (ok) {
        if (!ok) return;
        YB.openConfirmPasswordModal(
          action + " " + YB.maskEmail(email) + "？请输入当前管理员密码确认。",
          function (pw) {
            run(post(path(email, "/role"), { role: newRole, confirm_password: pw }),
              newRole === "admin" ? "已设为管理员" : "已取消管理员", false);
          });
      });
    }

    // 重置密码：新密码由页面模态收集并过完整策略，此处做二次鉴权 + 提交
    function resetPassword(uid, newPassword) {
      var email = emailOf(uid);
      if (!email || !newPassword) return;
      YB.openConfirmPasswordModal(
        "确认重置 " + YB.maskEmail(email) + " 的密码？重置后其旧会话立即失效。请输入当前管理员密码确认。",
        function (pw) {
          run(post(path(email, "/password"), { password: newPassword, confirm_password: pw }),
            "密码已重置", false);
        });
    }

    // mode=accounts_only（清空账号，用户保留可重新提交）| full（删除用户及其全部易班账号）
    // 删除不可逆：走 dangerousSubmit（后端按档位决定要口令还是倒计时确认），
    // 由响应 reason 分流，前端不判断档位。
    function deleteUser(uid, mode) {
      var email = emailOf(uid);
      if (!email) return;
      var full = mode === "full";
      YB.confirmDialog({
        title: full ? "删除用户" : "清空账号",
        body: full
          ? "确定删除 " + YB.maskEmail(email) + " 及其全部易班账号吗？此操作不可恢复。"
          : "确定清空 " + YB.maskEmail(email) + " 的易班账号吗？将解除签到服务，用户账号保留、可重新提交。",
        confirmText: full ? "删除用户" : "清空账号", danger: true
      }).then(function (ok) {
        if (!ok) return;
        run(YB.dangerousSubmit({
          path: path(email, "/delete"),
          body: { mode: mode },
          desc: (full ? "完全删除用户 " : "清空用户账号 ") + YB.maskEmail(email) + "？请输入当前管理员密码确认。",
          delayDesc: full
            ? "删除 " + YB.maskEmail(email) + " 及其全部易班账号不可恢复。确认继续？"
            : "清空 " + YB.maskEmail(email) + " 的易班账号。确认继续？"
        }), full ? "已删除用户" : "已清空账号", false);
      });
    }

    // 已注销用户立即物理清除：仅主管理员（后端 403 兜底）。不可逆，走 dangerousSubmit。
    function purge(uid) {
      var email = emailOf(uid);
      if (!email) return;
      YB.confirmDialog({
        title: "立即清除",
        body: "确定立即彻底清除 " + YB.maskEmail(email) + " 吗？将物理删除用户、其易班账号与自选签到时间，不可恢复。",
        confirmText: "立即清除", danger: true
      }).then(function (ok) {
        if (!ok) return;
        run(YB.dangerousSubmit({
          path: "/api/users/deleted/purge",
          body: { emails: [email] },
          desc: "再次确认：彻底清除 " + YB.maskEmail(email) + "？请输入当前管理员密码确认。",
          delayDesc: "彻底清除 " + YB.maskEmail(email) + " 将物理删除用户、其易班账号与自选签到时间，不可恢复。确认继续？"
        }), "已彻底清除");
      });
    }

    // 批量目标收集 + 单次上限前置拦截（与后端 BATCH_OP_LIMIT 同口径）
    function batchEmails(uids) {
      if (!uids || !uids.length) { YB.toast.error("请先勾选要操作的用户"); return null; }
      if (uids.length > LIMIT) { YB.toast.error("单次最多操作 " + LIMIT + " 个用户，请分批处理"); return null; }
      var emails = uids.map(emailOf).filter(Boolean);
      if (!emails.length) { YB.toast.error("选中项已失效，请刷新后重试"); return null; }
      return emails;
    }

    function batchReset(uids, newPassword) {
      var emails = batchEmails(uids);
      if (!emails || !newPassword) return;
      YB.openConfirmPasswordModal(
        "确认批量重置 " + emails.length + " 个用户的新密码？重置后其旧会话立即失效。请输入当前管理员密码确认。",
        function (pw) {
          run(post("/api/users/batch", {
            action: "reset_password", emails: emails, password: newPassword, confirm_password: pw
          }), "已重置密码");
        });
    }

    function batchDelete(uids) {
      var emails = batchEmails(uids);
      if (!emails) return;
      YB.confirmDialog({
        title: "批量删除用户",
        body: "确定删除选中的 " + emails.length + " 个用户及其全部易班账号吗？此操作不可恢复。",
        confirmText: "删除", danger: true
      }).then(function (ok) {
        if (!ok) return;
        run(YB.dangerousSubmit({
          path: "/api/users/batch",
          body: { action: "delete", emails: emails },
          desc: "再次确认：删除 " + emails.length + " 个用户？请输入当前管理员密码确认。",
          delayDesc: "删除这 " + emails.length + " 个用户及其全部易班账号不可恢复。确认继续？"
        }), "已删除用户");
      });
    }

    function batchPurge(uids) {
      var emails = batchEmails(uids);
      if (!emails) return;
      YB.confirmDialog({
        title: "批量彻底清除",
        body: "确定彻底清除选中的 " + emails.length + " 个已注销用户吗？将物理删除用户、易班账号与自选时间，不可恢复。",
        confirmText: "彻底清除", danger: true
      }).then(function (ok) {
        if (!ok) return;
        run(YB.dangerousSubmit({
          path: "/api/users/deleted/purge",
          body: { emails: emails },
          desc: "再次确认：彻底清除 " + emails.length + " 个已注销用户？请输入当前管理员密码确认。",
          delayDesc: "彻底清除这 " + emails.length + " 个已注销用户将物理删除用户、易班账号与自选时间，不可恢复。确认继续？"
        }), "已彻底清除");
      });
    }

    return {
      role: role,
      resetPassword: resetPassword,
      deleteUser: deleteUser,
      purge: purge,
      batchReset: batchReset,
      batchDelete: batchDelete,
      batchPurge: batchPurge
    };
  }

  YB.userOps = { create: create };
})();
