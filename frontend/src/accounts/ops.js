/* 账号管理页（管理端 /work/accounts）全部写操作的统一入口。
   挂载到 window.YB.accountOps；**刻意保持 classic script 形态**（不是 ESM），公开面
   YB.accountOps.create(ctx) 与迁移前逐字一致。

   ## 为什么这个文件是纯 JS 而不是 TS
   `tests/test_account_ops_reentry.py` 会把**本文件整段源码**放进 node 里执行（只桩一个
   `window.YB` 与 ctx），真调 create(ctx) 并断言：在途中的第二次触发（单条 restore/move 与
   批量 batch）不得再发请求、在途结束后必须放行。`tests/test_delay_ack_frontend.py` 另钉
   「单条 purge 与批量 purge 都走 dangerousSubmit」。用 TS（import 桥接外壳）会让那段真跑
   失效——抽出来的源码在 node 里跑不起来。故与 `src/users/ops.js` 同一处置：写操作链路留在
   纯 JS，由 Python 侧真跑钉住；Vue 侧只 `import "./ops.js"` 触发注册，再经 window.YB 取用。
   改动本文件前先读那两个测试。

   把「确认/口令鉴权 → 调接口 → 成功提示 → 刷新」这条固定链路收在一处，避免单条与批量
   各写一遍；页面只提供 ctx（状态与刷新回调），不关心网络细节。

   防错位：按 idx 的写操作一律携带列表下发的脱敏 phone（后端 _stale_idx_guard 双侧归一
   比对），列表漂移时 409；批量额外用 phones 与 ids 对齐，单次 <= BATCH_OP_LIMIT。
   脱敏：手动签到前按需取详情拿完整手机号，完整号只在请求体里流转。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var LIMIT = 10; // BATCH_OP_LIMIT，与后端 /api/accounts/batch 一致

  function sanitizeReason(v) {
    // 换行与控制符替换为空格（后端同口径），去首尾并截断 100
    return String(v == null ? "" : v).replace(/[\r\n\t\v\f\u0000-\u001f]+/g, " ").trim().slice(0, 100);
  }

  function create(ctx) {
    // 防重入：写操作在途时后续触发一律早退。此前只置 ctx.busy（页面拿它暂停轮询），
    // 不挡重复点击——restore/move/signin 这类无确认弹窗的入口双击即重发。
    var inflight = false;
    function fail(e) {
      // 用户取消弹窗（dangerousSubmit 以 canceled 标记拒绝）：不是失败，不提示
      if (e && e.canceled) return;
      YB.toast.error((e && e.message) || "操作失败，请稍后再试");
    }
    // 统一链路：置忙 → 请求 → 成功提示 + 刷新 → 复位。fallback 为空表示不弹成功提示。
    // suffix 追加在（后端 msg 或 fallback）之后，用于「软删除可恢复」这类固定补充说明。
    // after 在刷新完成后执行（ctx.refresh 需返回 Promise），用于对新生行的就地反馈。
    // makeReq 是**延迟构造**请求的 thunk：若把已构造的 promise 当参数传入，请求在进入
    // 本函数前就已发出，守卫形同虚设。
    function run(makeReq, fallback, after, suffix) {
      if (inflight) return;
      inflight = true;
      ctx.busy(true);
      return makeReq().then(function (data) {
        if (fallback || suffix) {
          YB.toast.success(((data && data.msg) || fallback || "操作成功") + (suffix || ""));
        }
        var ref = ctx.refresh();
        if (after) return Promise.resolve(ref).then(function () { after(data); });
      }).catch(fail).then(function () { inflight = false; ctx.busy(false); });
    }

    // 就地反馈（移动后行高亮 / 手动签到的「待签中」）**只回调组件、不碰 DOM**：
    // 架构原则是口径/写操作层只管网络与状态，渲染归 Vue 响应式状态。ctx.onFlash /
    // ctx.onSigning 可选，缺失即降级为无反馈（如 node 真跑测试的桩 ctx）。
    function notifyFlash(index) {
      if (index == null || !ctx.onFlash) return;
      ctx.onFlash(index);
    }
    function notifySigning(index) {
      if (index == null || !ctx.onSigning) return;
      ctx.onSigning(index);
    }

    function review(a, action) {
      var body = { action: action, phone: a.phone };
      var ask;
      if (action === "reject") {
        ask = YB.promptDialog({
          title: "驳回账号",
          label: "驳回理由（用户会看到，最多 100 字）",
          placeholder: "请填写驳回理由",
          maxlength: 100, required: true, requiredMessage: "驳回理由不能为空",
          confirmText: "驳回", cancelText: "取消"
        }).then(function (v) {
          if (v == null) return null;
          var reason = sanitizeReason(v);
          if (!reason) { YB.toast.error("驳回理由不能为空"); return null; }
          body.reason = reason;
          return true;
        });
      } else {
        ask = YB.confirmDialog({
          title: "通过账号",
          body: "确定通过「" + a.display_name + "」(" + a.phone + ") 吗？通过后将参与定时签到。",
          confirmText: "通过"
        });
      }
      ask.then(function (ok) {
        if (!ok) return;
        run(function () { return YB.api("POST", "/api/accounts/" + a.index + "/review", body); },
          action === "approve" ? "已通过" : "已拒绝");
      });
    }

    function remove(a) {
      YB.confirmDialog({
        title: "删除账号",
        body: "确定删除「" + a.display_name + "」（" + a.phone + "）吗？\n移入『待删除账号』，7 天内可恢复。",
        confirmText: "删除", danger: true
      }).then(function (ok) {
        if (!ok) return;
        run(function () { return YB.api("DELETE", "/api/accounts/" + a.index, { phone: a.phone }); },
          "已删除账号", null, " · 可在『待删除账号』恢复（7 天内）");
      });
    }

    function restore(a) {
      run(function () {
        return YB.api("POST", "/api/accounts/" + a.index + "/restore", { phone: a.phone });
      }, "已恢复");
    }

    // 彻底删除账号不可逆：走 dangerousSubmit，由后端响应 reason 决定要口令还是倒计时确认
    function purge(a) {
      run(function () {
        return YB.dangerousSubmit({
          path: "/api/accounts/" + a.index + "/purge",
          body: { phone: a.phone },
          desc: "彻底删除「" + a.display_name + "」（" + a.phone + "）？凭据将被物理清除，不可恢复！请输入当前管理员密码确认。",
          delayDesc: "彻底删除「" + a.display_name + "」（" + a.phone + "）会物理清除其凭据，不可恢复。确认继续？"
        });
      }, "已彻底删除");
    }

    function move(a, dir) {
      // 行重排后位置可能落在视口外：成功提示 + 新行短暂高亮，避免用户重复点击
      run(function () {
        return YB.api("POST", "/api/accounts/" + a.index + "/move", { dir: dir, phone: a.phone });
      }, dir === -1 ? "已上移" : "已下移", function () { notifyFlash(a.index); });
    }

    function signin(a) {
      // 手动签到需要完整手机号：按需取详情，完整号只在本次请求体内使用
      if (inflight) return;
      inflight = true;
      ctx.busy(true);
      YB.api("GET", "/api/accounts/" + a.index + "/detail").then(function (d) {
        var full = d && d.account && d.account.phone;
        if (!full) throw new Error("账号信息不完整，请刷新后重试");
        return YB.api("POST", "/api/signin", { phone: full });
      }).then(function (data) {
        YB.toast.success((data && data.msg) || "已触发手动签到");
        // 先就地显示「待签中」，随后重拉真实状态，不等 10s 轮询
        notifySigning(a.index);
        setTimeout(function () { ctx.refresh(); }, 1000);
      }).catch(fail).then(function () { inflight = false; ctx.busy(false); });
    }

    function batch(action, ids, phones) {
      if (!ids.length) { YB.toast.error("请先勾选要操作的账号"); return; }
      if (ids.length > LIMIT) { YB.toast.error("单次最多操作 " + LIMIT + " 个账号，请分批处理"); return; }
      if (action === "signin") {
        YB.confirmDialog({
          title: "批量手动签到",
          body: "确定对选中的 " + ids.length + " 个账号执行手动签到吗？将按顺序逐个执行。",
          confirmText: "开始签到"
        }).then(function (ok) {
          if (ok) submit("/api/signin/batch", { ids: ids, phones: phones });
        });
        return;
      }
      var body = { action: action, ids: ids, phones: phones };
      if (action === "reject") {
        YB.promptDialog({
          title: "批量驳回",
          label: "驳回理由（用户会看到，最多 100 字）",
          placeholder: "请填写共同理由", maxlength: 100, required: true,
          requiredMessage: "驳回理由不能为空", confirmText: "驳回", cancelText: "取消"
        }).then(function (v) {
          if (v == null) return;
          var reason = sanitizeReason(v);
          if (!reason) { YB.toast.error("驳回理由不能为空"); return; }
          body.reason = reason;
          submit("/api/accounts/batch", body);
        });
        return;
      }
      if (action === "purge") {
        submit("/api/accounts/batch", body, {
          desc: "彻底删除选中的 " + ids.length + " 个账号？凭据将被物理清除，不可恢复！请输入当前管理员密码确认。",
          delayDesc: "彻底删除选中的 " + ids.length + " 个账号会物理清除其凭据，不可恢复。确认继续？"
        });
        return;
      }
      var labels = { approve: "通过", delete: "删除", restore: "恢复" };
      YB.confirmDialog({
        title: "批量" + (labels[action] || "操作"),
        body: "确定对选中的 " + ids.length + " 个账号执行「" + (labels[action] || action) + "」吗？",
        confirmText: labels[action] || "确定", danger: action === "delete"
      }).then(function (ok) {
        if (ok) submit("/api/accounts/batch", body);
      });
    }

    // gated（可选）：不可逆批量操作传 {desc, delayDesc}，改走 dangerousSubmit 由后端
    // 响应 reason 分流（口令 / 倒计时确认）；不传则沿用原请求路径。
    function submit(path, body, gated) {
      if (inflight) return;
      inflight = true;
      ctx.busy(true);
      var isDelete = !!(body && body.action === "delete");
      var req = gated
        ? YB.dangerousSubmit({ path: path, body: body, desc: gated.desc, delayDesc: gated.delayDesc })
        : YB.api("POST", path, body);
      req.then(function (data) {
        YB.toast.success(((data && data.msg) || "操作成功")
          + (isDelete ? " · 可在『待删除账号』恢复（7 天内）" : ""));
        ctx.onBatchSuccess();
        ctx.refresh();
        // 批量删除后就地给出恢复入口，避免入口只存在于另一个标签页
        if (isDelete && ctx.onBatchDelete) ctx.onBatchDelete();
      }).catch(function (e) {
        // 409 = 列表在快照后漂移，后端文案已提示刷新
        fail(e);
      }).then(function () { inflight = false; ctx.busy(false); });
    }

    return { review: review, remove: remove, restore: restore, purge: purge, move: move, signin: signin, batch: batch };
  }

  YB.accountOps = { create: create };
})();
