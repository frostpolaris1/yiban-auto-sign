/* 系统设置页（管理端 /work/settings）写操作链路——纯 JS（不是 TS），直读 `window.YB`。
   挂到 `window.YB.settingsOps`，由各 .vue 组件调用。

   为什么整条写链收在一处：legacy 七个组件各写一份保存/收尾（`settings-{schedule,notify,
   mail,quota,health,switches,executors}.js`），其中执行体的多段提交收尾与邮件"清空收件人"
   的顺序陷阱是两处独立实现的同类洞。迁移时把写链收进本文件：
     · 所有受门禁写操作走 `YB.dangerousSubmit`（先不带凭据发、由后端 reason 决定补哪种凭据，
       档位只存在于后端）；
     · 多段提交被打断（取消 / 被后端打回）时**必须重载视图并交代已落盘步数**——两条路径
       共用 `partialAfter`；
     · "先提示后重载会被 load() 末尾的清屏抹掉"这一顺序陷阱由 `finish` 统一承担。

   ⚠ 本文件被多个 Python 守卫按花括号配对**抽出函数真跑 / 静态钉点**：
     · tests/test_delay_ack_frontend.py：`_GATED_VUE` 要求出现 `dangerousSubmit(`；
       ExecutorSaveCancelTest 抽 `partialAfter`/`canceledAfter`/`failedAfter` 做静态钉点；
       MailClearAdminToTipTest 抽 `finish`/`clearAdminTo` 放进 node 真跑（钉"成功提示落在
       重载之后"）。
   故这些函数的**名字与关键书写形态**是契约：改名前先读那两个测试。

   权限：UI 禁用不是安全边界，后端对每个写点独立判档位（403/口令门）。本层只做编排。 */

(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  /* 当前动作的上下文（每次调用由组件注入）：isMaster / 各卡的状态回调。
     守卫的 node 替身也定义同名 `ctx`，故这里的函数一律按名引用它。 */
  var ctx = { isMaster: false };
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function count(v) { return Number(v) || 0; }
  function attr(v) { return v == null ? "" : String(v); }

  /* 就地提示 / 横幅：默认写各卡的 .set-tip / #set-exec-banner；ctx 可覆盖。 */
  function setTip(text, bad) {
    if (ctx && typeof ctx.tip === "function") { ctx.tip(text, bad); return; }
    var n = $("set-tip");
    if (!n) return;
    n.className = bad ? "set-tip set-bad" : "set-tip";
    n.textContent = text == null ? "" : String(text);
  }
  var BANNER_ICON = { success: "circle-check", danger: "circle-x", info: "info" };
  function banner(text, kind) {
    if (ctx && typeof ctx.banner === "function") { ctx.banner(text, kind); return; }
    var box = $("set-exec-banner");
    if (!box) return;
    var ico = $("set-exec-banner-ico");
    var body = $("set-exec-banner-body");
    if (ico) {
      ico.textContent = "";
      if (YB.iconEl) ico.appendChild(YB.iconEl(BANNER_ICON[kind] || "info"));
    }
    if (body) body.textContent = text == null ? "" : String(text);
    box.className = "alert " + (kind || "info");
    box.hidden = !text;
  }
  /* 视图重载：由组件注入（每卡自己的 load）。守卫替身里 load() 返回 resolved。
     可选 ctx：把目标卡透传进来，避免并发动作把模块级 ctx 指到别的卡后打错目标。 */
  function load(c) {
    var cc = c || ctx;
    return cc && typeof cc.load === "function" ? cc.load() : Promise.resolve(true);
  }
  function busySet(on, c) {
    busy = !!on;
    var cc = c || ctx;
    if (cc && typeof cc.setBusy === "function") cc.setBusy(!!on);
  }
  function toastInfo(msg) { if (YB.toast) YB.toast.info(msg); }
  function toastError(msg) { if (YB.toast) YB.toast.error(msg); }
  function isMaster() { return !!(ctx && ctx.isMaster); }

  /* =========================================================================
     执行体分区：受门禁写操作的统一入口 + 多段提交的收尾
     ========================================================================= */

  // 受门禁写操作的统一入口：先不带凭据发，由 core.js 的 dangerousSubmit 按后端 reason 补
  // 口令或倒计时确认（档位只存在于后端，本层不判断、也不预判要不要口令）。
  // `c` 是本次动作的卡片上下文：`ctx` 是模块级可变全局，并发的第二个动作（哪怕因 busy 直接
  // 返回）也可能把它改到别处；故整条 async 链在每次续跑前都用快照 `c` 复原，收尾才打对目标。
  function gated(opts, onOk, failWord, c) {
    return withBusy(function () {
      ctx = c;
      banner("提交中…", "info");
      return YB.dangerousSubmit(opts).then(function (d) {
        ctx = c;
        return load().then(function () { ctx = c; return onOk(d); });
      });
    }, c).catch(function (e) {
      ctx = c;
      if (e && e.canceled) return canceledAfter(e);
      return failedAfter(e, failWord);
    });
  }
  // 取消弹窗与"打到一半被打回"都不是"什么都没发生"：多段保存（改出口 + 拨开关）里先成功的
  // 步骤已经写进 `.env`，若照旧静默返回或只把错误留在横幅里，用户会以为整次保存没发生、
  // 而配置已经变了。两条路径共用同一收尾——先重载视图（页面显示库里的真实状态），确有部分
  // 写入时讲明不回滚；提示语按取消/失败取词，步数由 helper 回传（见 core.js 的 dangerousSubmit）。
  function partialAfter(done, text) {
    return load().then(function () {
      // 顺序不能反：load() 走 apply() 会清掉横幅，所以提示必须落在重载之后
      setTip(text ? text + "；本次保存的前 " + done + " 步已经写入配置（部分修改已提交、"
        + "不会回滚），上面显示的是配置的当前状态。" : "", !!text);
      return false;
    });
  }
  function canceledAfter(e) {
    var done = count(e && e.completed);
    return partialAfter(done, done > 0 ? "已取消" : "");
  }
  // 一步都没提交的失败无需重载（库里没变），提示照旧就地落横幅
  function failedAfter(e, failWord) {
    var done = count(e && e.completed);
    if (!done) { failTip(e, failWord); return false; }
    return partialAfter(done, (e && e.message) || (failWord + "失败，请稍后重试"));
  }
  function failTip(e, what) { setTip((e && e.message) || (what + "失败，请稍后重试"), true); }
  // 在途终态：按钮跟着 busy 灰掉，"点了没反应"变成"按钮灰着"
  function withBusy(fn, c) {
    if (busy) return Promise.resolve(false);
    busySet(true, c);
    function cleanup() { busySet(false, c); }
    return Promise.resolve().then(fn).then(function (ok) {
      cleanup();
      return ok;
    }, function (e) {
      cleanup();
      throw e;
    });
  }
  function note(d) { return attr(d && d.note) || "已写入配置"; }

  // 追加行：受门禁写操作（追加一定改配置），口令/确认由 helper 按后端 reason 收
  function executorsAddRow(c) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    if (!isMaster()) return Promise.resolve(false);
    return gated({
      method: "POST", path: "/api/scheduler/executors/rows", body: { type: "worker" },
      desc: "添加一行执行体（并行、默认直连）？追加行会改动执行体清单，请输入当前管理员密码确认。"
    }, function (d) {
      setTip("已添加「并行执行体 #" + (count(d && d.slot) + 1) + "」（默认直连）：" + note(d)
        + "。想给它单独出口，点那一行的「设置」；编号只增不复用，故障转移行也占一个编号"
        + "（它固定置顶、行名不带数字），所以并行行跳号是正常的、不影响运行"
        + "（没删过行却看到跳号，就是它在占号）。", false);
      return true;
    }, "添加", c);
  }

  // 改状态（停用/启用）：受门禁写操作，口令/确认由 helper 按后端 reason 收；成功/失败都走横幅
  function executorsChangeType(c, row, nextType, word) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    var slot = count(row && row.slot);
    var name = attr(row && row.title) || attr(row && row.label) || "执行体";
    return gated({
      method: "PUT", path: "/api/scheduler/executors/rows/" + slot,
      body: { type: nextType },
      desc: word + " " + name + "？请输入当前管理员密码确认。"
    }, function (d) {
      banner("已" + word + " " + name + "：" + note(d), "success");
      return true;
    }, word, c);
  }

  // 删行：槽位号不复用、出口配置一并删除，是破坏性动作——影响面由 confirmDialog 讲清
  function executorsRemoveRow(c, row) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    var local = c;
    var slot = count(row && row.slot);
    var name = attr(row && row.title) || attr(row && row.label) || "执行体";
    return YB.confirmDialog({
      title: "删除执行体",
      body: "删除 " + name + "（槽位 " + slot + "）？它的出口配置会一并删除、槽位号不保留；"
        + "只是暂时不用请改用「停用」。",
      confirmText: "删除", danger: true
    }).then(function (ok) {
      if (!ok) return false;
      ctx = local;
      return gated({
        method: "DELETE", path: "/api/scheduler/executors/rows/" + slot, body: {},
        desc: "删除 " + name + "（槽位 " + slot + "）？请输入当前管理员密码确认。"
      }, function (d) {
        banner("已删除 " + name + "：" + note(d), "success");
        return true;
      }, "删除", local);
    });
  }

  // 清除出口（改为直连）：受门禁写操作，影响面由 confirmDialog 讲清
  function executorsClearEgress(c, row) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    var local = c;
    var slot = count(row && row.slot);
    var title = attr(row && row.title) || attr(row && row.label) || "执行体";
    return YB.confirmDialog({
      title: "清除出口",
      body: "清除 " + title + " 的出口（改为直连）？原出口配置会从配置项里删掉，不可撤销。",
      confirmText: "清除", danger: true
    }).then(function (ok) {
      if (!ok) return false;
      ctx = local;
      return gated({
        method: "PUT", path: "/api/scheduler/executors/rows/" + slot, body: { proxy: "" },
        desc: "清除 " + title + " 的出口（改为直连）？请输入当前管理员密码确认。"
      }, function (d) {
        setTip("已清除出口：" + note(d), false);
        return true;
      }, "清除", local);
    });
  }

  // 行内保存：**改出口或开关进门禁，只改名不进**（后端 type/proxy 真变才判）；只改名直发。
  // requests 由组件按"本次实际要写的项"组好（行接口 + 开关接口），needsPw 决定走哪条。
  function executorsSaveRow(c, row, plan) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    var title = attr(row && row.title) || attr(row && row.label) || "执行体";
    if (!plan.needsPw) return executorsDirect(plan.requests[0], title, c);
    return gated({ requests: plan.requests, desc: plan.desc }, function (res) {
      banner("已保存 " + title + "：" + note(res && res[0]), "success");
      return true;
    }, "保存", c);
  }
  // 只改名：不进门的直接提交，成功/失败都走横幅
  function executorsDirect(req, title, c) {
    if (busy) return Promise.resolve(false);
    return withBusy(function () {
      ctx = c;
      banner("提交中…", "info");
      return YB.api(req.method, req.path, req.body).then(function (d) {
        ctx = c;
        return load().then(function () {
          ctx = c;
          banner("已保存 " + title + "：" + note(d), "success");
          return true;
        });
      });
    }, c).catch(function (e) { ctx = c; failTip(e, "保存"); return false; });
  }

  /* =========================================================================
     签到调度 / 容量配额 / 健康与探针：POST /api/settings 的受门禁保存
     ========================================================================= */

  // 调度：受门禁的保存（先不带凭据发，后端 reason 决定要不要口令）
  function scheduleSave(c, body) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    busySet(true, c);
    return YB.dangerousSubmit({
      method: "POST", path: "/api/settings", body: body,
      desc: "调度参数改动会影响全站何时签到（窗口、掐头去尾、账号间隔等），不合适的设置可能拉低成功率或被容量硬门拒绝。请输入当前管理员密码确认。"
    }).then(function (data) {
      ctx = c;
      if (typeof c.onSaved === "function") c.onSaved(data);
      setTip((data && data.msg) || "调度设置已保存", false);
      return true;
    }, function (e) {
      ctx = c;
      if (e && e.canceled) setTip("", false);
      else setTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      busySet(false, c);
      return ok;
    });
  }

  // 容量上限：受门禁的保存；成功后回调页面刷新容量状态（调度警示 / 执行体 KPI）
  function quotaSave(c, body) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    busySet(true, c);
    return YB.dangerousSubmit({
      method: "POST", path: "/api/settings", body: body,
      desc: "调整容量上限：不合适的设置可能影响新增注册/账号，是否继续？\n请输入当前管理员密码确认。"
    }).then(function (data) {
      ctx = c;
      setTip((data && data.msg) || "容量上限已保存", false);
      if (typeof c.onSaved === "function") c.onSaved(data);
      return true;
    }, function (e) {
      ctx = c;
      if (e && e.canceled) setTip("", false);
      else setTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      busySet(false, c);
      return ok;
    });
  }

  // 健康与探针：受门禁的保存
  function healthSave(c, body) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    busySet(true, c);
    return YB.dangerousSubmit({
      method: "POST", path: "/api/settings", body: body,
      desc: "探针与账号验证会让服务器对全站账号发起真实易班登录（与签到同一风控面）。请输入当前管理员密码确认。"
    }).then(function (data) {
      ctx = c;
      setTip((data && data.msg) || "探针设置已保存（将在设定时间后的调度周期自动执行）", false);
      return true;
    }, function (e) {
      ctx = c;
      if (e && e.canceled) { setTip("", false); return false; }
      setTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      busySet(false, c);
      return ok;
    });
  }

  /* =========================================================================
     消息推送
     ========================================================================= */

  function notifySave(c, payload) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    if (!isMaster()) return Promise.resolve(false);
    busySet(true, c);
    return YB.dangerousSubmit({
      method: "PUT", path: "/api/notify-config", body: payload,
      desc: "保存消息推送配置属于高危操作。\n请输入当前管理员密码确认。"
    }).then(function () {
      ctx = c;
      if (typeof c.onSaved === "function") c.onSaved();
      // 成功文案必须等回读完成后再写：load() 末尾 applyNotify() 无条件清空 sn-tip，
      // 先提示后重载会把刚落下的一句整条抹掉、看起来从未保存过。与邮件 finish 同顺序。
      return load().then(function () { ctx = c; setTip("推送配置已保存", false); return true; });
    }, function (e) {
      ctx = c;
      if (e && e.canceled) setTip("", false);
      else setTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      busySet(false, c);
      return ok;
    });
  }

  function notifyTest(c) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    if (!isMaster()) return Promise.resolve(false);
    busySet(true, c);
    setTip("发送中…", false);
    return YB.api("POST", "/api/notify-test").then(function (data) {
      ctx = c;
      setTip((data && data.msg) || "已发送", false);
      return true;
    }, function (e) {
      ctx = c;
      setTip((e && e.message) || "发送失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      busySet(false, c);
      return ok;
    });
  }

  /* =========================================================================
     邮件通知 / SMTP
     ========================================================================= */

  // 保存收尾：成功清空收件人输入并刷新，失败/取消只落提示行；按钮复位两种路径共用。
  // 提示必须落在调用方 load() 之后——load() 末尾无条件 setTip("", false) 清屏，先提示后重载
  // 会把刚落下的一句整条抹掉（"已保存"看起来从未出现过）。okText 让各动作有自己的成功文案。
  function finish(ok, err, canceled, okText) {
    if (ok) {
      if (ctx && typeof ctx.afterMailSave === "function") ctx.afterMailSave();
      setTip(okText || "邮件配置已保存", false);
    } else if (canceled) {
      setTip("", false);                     // 取消弹窗 = 本次不保存，不留"保存中…"
    } else {
      setTip((err && err.message) || "保存失败，请稍后重试", true);
    }
    busy = false;
    if (YB.setBusy) YB.setBusy("sm-save", false);
    return ok;
  }

  // 受门禁的保存（关全局通知 / 改收件人 / 改 SMTP 通道）：先不带凭据发，由后端 reason 定
  function mailGated(c, payload) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    if (!isMaster()) return Promise.resolve(false);
    busySet(true, c);
    return YB.dangerousSubmit({
      method: "PUT", path: "/api/mail-config", body: payload,
      desc: "保存邮件配置：关闭全局通知、修改告警收件人或更换 SMTP 通道属敏感操作。\n请输入当前管理员密码确认。"
    }).then(function () {
      ctx = c;
      return load().then(function () { ctx = c; return finish(true); });
    }, function (e) {
      ctx = c;
      return finish(false, e, !!(e && e.canceled));
    });
  }

  // 纯"开启"不带门禁（后端同口径：不给正常成功路径加摩擦）
  function mailDirect(c, payload) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    if (!isMaster()) return Promise.resolve(false);
    busySet(true, c);
    return YB.api("PUT", "/api/mail-config", payload).then(function () {
      ctx = c;
      return load().then(function () { ctx = c; return finish(true); });
    }, function (e) {
      ctx = c;
      return finish(false, e);
    });
  }

  function mailSave(c, payload) {
    var needPw = Object.prototype.hasOwnProperty.call(payload, "admin_to") ||
      Object.prototype.hasOwnProperty.call(payload, "smtps") || payload.enabled === false;
    return needPw ? mailGated(c, payload) : mailDirect(c, payload);
  }

  // 清空告警收件人：影响面由 confirmDialog 讲清，口令/确认交给统一 helper 按后端 reason 收。
  // 收尾复用 finish：成功提示必须落在 load() 之后（load() 末尾会无条件清屏），与保存路径同口径。
  function clearAdminTo(c) {
    if (c) ctx = c;
    if (busy || !(ctx && ctx.isMaster)) return Promise.resolve(false);
    var local = ctx;
    return YB.confirmDialog({
      title: "清空告警收件人",
      body: "清空后管理员告警邮件将无人接收（除非另有开启接收的管理员）。确定继续？",
      confirmText: "清空", danger: true
    }).then(function (ok) {
      if (!ok) return false;
      ctx = local;
      busy = true;
      return YB.dangerousSubmit({
        method: "PUT", path: "/api/mail-config", body: { admin_to: "" },
        desc: "再次确认：清空告警收件人？请输入当前管理员密码确认。"
      }).then(function () {
        ctx = local;
        return load().then(function () {
          ctx = local;
          return finish(true, null, false, "已清空告警收件人");
        });
      }, function (e) {
        ctx = local;
        return finish(false, e, !!(e && e.canceled));
      });
    });
  }

  /* =========================================================================
     公告（双人发布）
     ========================================================================= */

  // 只写草稿：保留后端 msg（"草稿已保存，待主管理员发布" / "草稿已清除（线上公告未变…）"）。
  function announcementSaveDraft(c, text) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    busySet(true, c);
    return YB.api("PUT", "/api/announcement", { text: text }).then(function (data) {
      ctx = c;
      return load().then(function () {
        ctx = c;
        setTip((data && data.msg) || (text ? "草稿已保存" : "草稿已清除"), false);
        return true;
      });
    }, function (e) {
      ctx = c;
      setTip((e && e.message) || "保存失败，请稍后重试", true);
      return false;
    }).then(function (ok) {
      busySet(false, c);
      return ok;
    });
  }

  // 发布/下线：先确认影响面 → （未保存的编辑先自动落草稿）→ 受门禁提交发布那一步。
  function announcementPublish(c, opts) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    if (!isMaster()) return Promise.resolve(false);
    var local = c;
    var offline = !!opts.offline;
    return YB.confirmDialog({
      title: offline ? "下线线上公告" : "发布公告",
      body: offline
        ? "下线后所有页面顶部（含登录页）的公告都会消失。确认继续？"
        : "发布后公告会立即出现在所有页面顶部（含登录页）。确认继续？",
      confirmText: offline ? "下线" : "发布",
      danger: offline
    }).then(function (ok) {
      if (!ok) return false;
      ctx = local;
      busySet(true, local);
      setTip("发布中…", false);
      // 草稿落盘不改变线上公告、不进门禁，故它先直发；门禁只落在发布那一步
      var chain = Promise.resolve();
      if (opts.dirty) {
        chain = YB.api("PUT", "/api/announcement", { text: opts.draftText }).then(function () {
          ctx = local;
          if (typeof c.onDraftSaved === "function") c.onDraftSaved();
        });
      }
      return chain.then(function () {
        ctx = local;
        return YB.dangerousSubmit({
          path: "/api/announcement/publish", body: {},
          desc: (offline ? "下线全站公告" : "发布全站公告") + "会影响所有访问者。请输入当前管理员密码确认。"
        });
      }).then(function (data) {
        ctx = local;
        if (YB.toast) YB.toast.success((data && data.msg) || "已发布");
        if (YB.applyAnnouncementText) YB.applyAnnouncementText(data && data.text);
        return load().then(function () {
          ctx = local;
          setTip((data && data.msg) || "已发布", false);
          return true;
        });
      }, function (e) {
        // 取消弹窗不是失败：不提示、失败不动已保存的草稿（门禁失败已在弹窗内显示）
        ctx = local;
        if (!(e && e.canceled)) setTip((e && e.message) || "发布失败，请稍后重试", true);
        return false;
      }).then(function (ok) {
        busySet(false, local);
        return ok;
      });
    });
  }

  function announcementClear(c) {
    ctx = c;
    if (busy) return Promise.resolve(false);
    return YB.confirmDialog({
      title: "清除草稿",
      body: "清除后草稿会被删除，线上公告不受影响；如需撤下线上公告请点「下线线上公告」。确定继续？",
      confirmText: "清除", danger: true
    }).then(function (ok) {
      if (!ok) return false;
      return announcementSaveDraft(c, "");
    });
  }

  /* =========================================================================
     系统开关（按变更方向分权）
     ========================================================================= */

  // 危险开关：确认写明影响范围 → 提交（只提交被改的那一个字段）。
  // 凭据交给统一 helper：先不带凭据发，后端按档位与风控回 reason 才补口令或倒计时确认。
  function switchesPause(c, field, next) {
    ctx = c;
    var what = field === "global_pause" ? "签到" : "注册";
    var impact = field === "global_pause"
      ? (next ? "所有账号将停止自动签到：正在运行的一轮会跑完，手动签到不受影响，可随时恢复。确认继续？"
        : "下一轮自动签到将恢复执行。确认继续？")
      : (next ? "登录页将关闭注册入口，新用户无法自助注册；已注册用户登录不受影响。确认继续？"
        : "登录页将恢复注册入口。确认继续？");
    return YB.confirmDialog({
      title: (next ? "暂停" : "恢复") + what, body: impact,
      confirmText: next ? "暂停" : "恢复", danger: next
    }).then(function (ok) {
      if (!ok) return false;
      var body = {};
      body[field] = next ? 1 : 0;
      return YB.dangerousSubmit({
        path: "/api/settings", body: body,
        desc: "确认" + (next ? "暂停" : "恢复") + what + "？请输入当前管理员密码确认。",
        // 倒计时确认只对不可逆的急停出现；其余方向后端不会下发 delay_ack_required
        delayDesc: field === "global_pause" && next
          ? "暂停签到会让所有账号停止自动签到（正在运行的一轮会跑完），确认继续？" : null
      }).then(function () {
        if (typeof c.onPaused === "function") c.onPaused(field, next);
        if (YB.toast) YB.toast.success(next ? what + "已暂停" : what + "已恢复");
        return true;
      }, function (e) {
        if (e && e.canceled) return false;   // 取消弹窗：不是失败
        toastError((e && e.message) || "操作失败，请稍后再试");
        return false;
      });
    });
  }

  /* =========================================================================
     容量建议与耗时实测
     ========================================================================= */

  // 实测（POST /measure）：真的会用真实账号访问一次易班（只读、不签到）。
  // **口令门**：后端口径是"不改配置 → 不要求 confirm_password"（与手动签到同档，只判主
  // 管理员 + 冷却），故这里**不放口令框**——前端弹一个后端不校验的口令框就是"假门"。
  // 保留一道**诚实的二次确认**：一次实测会拿真实账号真登录一次，值得让操作者按一下。
  function quotaMeasure(c) {
    if (busy) return Promise.resolve(false);
    ctx = c;
    if (!isMaster()) return Promise.resolve(false);
    var local = c;
    return YB.confirmDialog({
      title: "现场实测单账号耗时？",
      body: "会用一位真实账号登录一次易班（只读、不签到，不写签到状态、不占领取池），"
        + "并占用全局实测冷却。确认后立即开始。",
      confirmText: "开始实测"
    }).then(function (ok) {
      if (!ok) return false;
      ctx = local;
      busySet(true, local);
      return YB.api("POST", "/api/scheduler/executors/measure", {}).then(function (d) {
        ctx = local;
        if (typeof c.onMeasure === "function") c.onMeasure(d, null);
        return true;
      }, function (e) {
        ctx = local;
        if (typeof c.onMeasure === "function") c.onMeasure(null, e);
        return false;
      }).then(function (ok2) {
        busySet(false, local);
        return ok2;
      });
    });
  }

  YB.settingsOps = {
    // 执行体
    executorsAddRow: executorsAddRow,
    executorsChangeType: executorsChangeType,
    executorsRemoveRow: executorsRemoveRow,
    executorsClearEgress: executorsClearEgress,
    executorsSaveRow: executorsSaveRow,
    // 设置保存
    scheduleSave: scheduleSave,
    quotaSave: quotaSave,
    healthSave: healthSave,
    notifySave: notifySave,
    notifyTest: notifyTest,
    mailSave: mailSave,
    mailClear: clearAdminTo,
    announcementSaveDraft: announcementSaveDraft,
    announcementPublish: announcementPublish,
    announcementClear: announcementClear,
    switchesPause: switchesPause,
    quotaMeasure: quotaMeasure,
    // 供组件查询在途状态
    isBusy: function () { return busy; },
  };
})();
