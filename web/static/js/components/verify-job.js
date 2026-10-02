/* 在线校验任务状态（用户端 /user 与 /mine 共用的唯一实现）。
   挂载到 window.YB.verifyJob；classic script，公开面 start(jobId, opts) → { stop }。

   ## 为什么需要它
   提交账号后端会**异步**做一次在线校验（POST /api/my-accounts 回 job_id），
   并提供 GET /api/verify-jobs/<id> 查状态、DELETE 同路径取消未开工的任务。
   在本组件之前，这两个端点前端从未调用：提交者只看到"已提交，等待管理员审核"，
   校验成功与否对他完全不可见——后端做了一份工，用户拿不到结果。

   ## 状态机（取值与 yiban/store/verify_jobs.py 一一对应，勿自造）
     pending    已排队，尚未开工          → 可取消
     running    正在校验                  → 不可取消（后端只允许取消 pending）
     done       校验通过                  （终态）
     rejected   校验未通过（error 带原因） （终态）
     cancelled  已取消                    （终态）

   ## 诚实性约定
   · 轮询有上界（MAX_POLLS），到顶显示"仍在进行，可稍后刷新查看"而不是无限转圈；
   · 任何一次查询失败都**如实显示失败**并停止，不假装任务已结束；
   · 账号本身在提交那一刻就已落库（status=pending 等审核），校验结果只影响审核判断，
     因此本组件全程不阻断提交、不回滚账号。

   opts：
     hostSel   承载提示的容器选择器（默认 #my-verify-job）
     onFinish  终态回调（可选），参数为归一化后的 { status, error, phone } */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  // 退避序列：前几次密（校验通常几秒内出结果），随后拉长避免长期占请求
  var POLL_MS = [1200, 2000, 3000, 5000, 8000, 10000];
  var MAX_POLLS = 24;          // 约 2~3 分钟；超出即交还用户，不无限轮询
  var TERMINAL = { done: 1, rejected: 1, cancelled: 1 };

  var live = null;   // 当前在跑的任务句柄；同一时刻只跟一个，避免两个提示互相覆盖

  function start(jobId, opts) {
    opts = opts || {};
    if (!jobId) return { stop: function () {} };
    stop();                       // 上一个还在轮询就收掉（重复提交/重新打开表单）

    var host = YB.$(opts.hostSel || "#my-verify-job");
    if (!host) return { stop: function () {} };

    var textEl = host.querySelector("[data-verify-text]") || host;
    var cancelBtn = host.querySelector("[data-verify-cancel]");
    var timer = null;
    var tries = 0;
    var stopped = false;
    var job = null;               // 最近一次服务端确认的状态，取消时据此决定可否点

    function clearTimer() { if (timer) { clearTimeout(timer); timer = null; } }

    function render(text, bad, canCancel) {
      textEl.textContent = text || "";
      // 失败态用 .set-warn（项目既有的就地告警形态），其余用 .set-tip
      host.classList.toggle("set-warn", !!bad);
      if (cancelBtn) {
        cancelBtn.hidden = !canCancel;
        if (!canCancel) cancelBtn.disabled = false;
      }
      host.hidden = !text;
    }

    function finish(status, err, phone) {
      clearTimer();
      if (status === "done") {
        render("在线校验通过" + (phone ? "（" + phone + "）" : "") + "。等待管理员审核后即可自动签到。", false, false);
      } else if (status === "cancelled") {
        render("已取消本次在线校验。账号仍已提交，管理员审核时可手动核对。", false, false);
      } else {
        // rejected：error 是后端给的原因，照实显示；为空则不编造原因
        render("在线校验未通过" + (err ? "：" + err : "") + "。可修正账号信息后重新提交。", true, false);
      }
      if (opts.onFinish) opts.onFinish({ status: status, error: err || "", phone: phone || "" });
    }

    function poll() {
      if (stopped) return;
      YB.api("GET", "/api/verify-jobs/" + encodeURIComponent(jobId)).then(function (data) {
        if (stopped) return;
        job = (data && data.job) || {};
        var st = job.status || "";
        tries++;
        if (TERMINAL[st]) { finish(st, job.error, job.phone); return; }

        // 未到终态：pending 可取消，running 不可（与后端只允许取消 pending 一致）
        render(
          st === "running" ? "正在在线校验账号信息…" : "已排上在线校验，等待执行…",
          false, st === "pending"
        );
        if (tries >= MAX_POLLS) {
          clearTimer();
          render("在线校验仍在进行，可稍后刷新本页查看结果。", false, false);
          return;
        }
        timer = setTimeout(poll, POLL_MS[Math.min(tries, POLL_MS.length - 1)]);
      }).catch(function (e) {
        if (stopped) return;
        clearTimer();
        // 查不到/无权/网络失败：如实说明并停止，不把"查不到"说成"已结束"
        var msg = (e && e.status === 404) ? "校验任务已不存在或已过期"
                : (e && e.status === 403) ? "无权查看该校验任务"
                : "校验状态查询失败，请稍后刷新查看";
        render(msg, true, false);
      });
    }

    function onCancel() {
      if (!job || job.status !== "pending") return;
      cancelBtn.disabled = true;
      YB.api("DELETE", "/api/verify-jobs/" + encodeURIComponent(jobId)).then(function (data) {
        if (stopped) return;
        var j = (data && data.job) || {};
        finish(j.status || "cancelled", j.error, j.phone);
      }).catch(function (e) {
        if (stopped) return;
        cancelBtn.disabled = false;
        // 409 = 已被后台线程抢走开工，取消不再成立：改查一次真实状态，别停在"可取消"
        if (e && e.status === 409) poll();
        else render("取消失败：" + ((e && e.message) || "请稍后重试"), true, job.status === "pending");
      });
    }

    function stop() {
      stopped = true;
      clearTimer();
    }

    if (cancelBtn) cancelBtn.addEventListener("click", onCancel);
    render("正在查询校验状态…", false, false);
    poll();

    live = { stop: stop };
    return { stop: stop };
  }

  function stop() { if (live) { live.stop(); live = null; } }

  YB.verifyJob = { start: start, stop: stop };
})();
