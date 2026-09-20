/* 系统设置 · 消息推送段（管理端 /work/settings 的「通知通道」分区）。

   挂载到 window.YB.settingsNotify；classic script。邮件段拆在 settings-mail.js，
   两段各占一张卡，故非主管理员的禁用由本组件对整卡统一处理。

   权限（读与写不同档，别写成"GET 也会 403"）：
     · 读 GET /api/notify-config —— 任意管理员可读通道状态与规则配置字段；额度**余量**
       （daily_remaining / urgent_daily_remaining）仅主管理员，无权查看时后端置 null 并
       恒定下发 quota_visible=false（本组件先看 quota_visible 再决定显示口径，不按 null 判）。
     · 写与测试 —— 仅主管理员，且关闭通道、更换/清空密钥、调整额度节流都要
       confirm_password（后端 _high_risk_gate：值**真的变了**才要，同值提交不要求；
       UI 不是安全边界）。
   脱敏：密钥只读展示 secret_masked，输入框恒为空（留空=不改动），绝不回显。

   保存语义（与全页统一）：改动只标脏（脏徽标 + 保存按钮出现），点「保存推送配置」
   才提交，只发送相对快照真正变化的字段。
   对外面：mount/load/apply(load 同义)、save() → Promise<boolean>、isDirty()。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var isMaster = false;
  var snap = null;
  var busy = false;
  var dirty = false;

  function $(id) { return document.getElementById(id); }
  function setHidden(el, hidden) { if (el) el.hidden = !!hidden; }
  function setTip(text, bad) { YB.setTip("sn-tip", text, bad); }
  function markDirty() {
    if (dirty) return;
    dirty = true;
    setHidden($("sn-save"), false);
    setHidden($("sn-save-hint"), false);   // 说明与按钮同显隐：不指向看不见的按钮
    setHidden($("sn-dirty"), false);
  }
  function clearDirty() {
    dirty = false;
    setHidden($("sn-save"), true);
    setHidden($("sn-save-hint"), true);
    setHidden($("sn-dirty"), true);
  }
  // 权限说明追加到被禁用控件的 aria-describedby：保留控件原有说明（如 info-tip 的浮层），
  // 读屏聚焦/浏览到禁用控件时能听到"为什么禁用"。
  function associate(el, id) {
    var ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    if (ids.indexOf(id) === -1) { ids.push(id); el.setAttribute("aria-describedby", ids.join(" ")); }
  }
  function disableAll(root, descId) {
    if (!root) return;
    [].forEach.call(root.querySelectorAll("input,select,button,textarea"), function (n) {
      // .info-tip 是"为什么禁用"的说明入口，禁用后键盘/触摸都打不开（只剩鼠标 hover），
      // 恰恰把唯一的解释渠道掐掉了 —— 权限禁用不得连带禁用帮助入口。
      if (n.closest && n.closest(".info-tip")) return;
      n.disabled = true;
      if (descId) associate(n, descId);
    });
  }
  function value(id) { return ($(id) || {}).value || ""; }

  function renderStatus(data) {
    var el = $("sn-status");
    if (!el) return;
    var parts = [];
    if (data.enabled) {
      parts.push("已开启（" + (data.type === "serverchan" ? "Server酱" : "自定义地址") +
        "，密钥 " + (data.secret_masked || "已配置") + "）");
      if (data.urgent_only) parts.push("仅推送重要告警");
    } else {
      parts.push(data.configured ? "已配置但不可用（密钥缺失或解密失败，请重新填写密钥）" : "未配置");
    }
    // 额度余量按 quota_visible 分支：后端对无权查看者把 remaining 置 null，而 null 的
    // 正常语义是"上限为 0（不限）"——两者恰好相反，只按 null 判会把"无权查看"显示成"不限"。
    if (data.quota_visible === false) {
      parts.push("今日额度：仅主管理员可见");
    } else {
      var g = data.daily_remaining == null ? "不限" : data.daily_remaining + " 条";
      var u = data.urgent_daily_remaining == null ? "不限" : data.urgent_daily_remaining + " 条";
      parts.push("今日额度：非紧急剩余 " + g + " / 紧急剩余 " + u);
    }
    el.textContent = parts.join("；");
  }

  // 读取失败就地提示 + 提供重试（不再静默吞掉：用户看不到"配置其实是旧的/空的"）
  function showLoadError(msg) {
    var n = $("sn-tip");
    if (!n) return;
    n.className = "set-tip set-bad";
    n.textContent = (msg || "消息推送配置加载失败，请稍后重试") + " ";
    var btn = YB.el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "重试" });
    btn.addEventListener("click", function () {
      n.className = "set-tip";
      n.textContent = "";
      load();
    });
    n.appendChild(btn);
  }

  function load() {
    if (!isMaster) return Promise.resolve();
    return YB.api("GET", "/api/notify-config").then(function (data) {
      snap = {
        type: data.type || "",
        cooldown: data.cooldown != null ? Number(data.cooldown) : null,
        urgent_only: !!data.urgent_only,
        daily_max: data.daily_max != null ? Number(data.daily_max) : null,
        urgent_daily_max: data.urgent_daily_max != null ? Number(data.urgent_daily_max) : null,
        configured: !!data.configured
      };
      YB.selectField.set("sn-type", snap.type);
      var sec = $("sn-secret"); if (sec) sec.value = "";
      var cd = $("sn-cooldown"); if (cd && snap.cooldown != null) cd.value = String(snap.cooldown);
      var ur = $("sn-urgent"); if (ur) ur.checked = snap.urgent_only;
      var dm = $("sn-daily-max"); if (dm && snap.daily_max != null) dm.value = String(snap.daily_max);
      var um = $("sn-urgent-max"); if (um && snap.urgent_daily_max != null) um.value = String(snap.urgent_daily_max);
      renderStatus(data);
      clearDirty();
      setTip("", false);
    }).catch(function (e) {
      showLoadError((e && e.message) || "消息推送配置加载失败，请稍后重试");
    });
  }

  function collect() {
    var body = {};
    var t = value("sn-type");
    var secret = value("sn-secret").trim();
    var cd = parseInt(value("sn-cooldown"), 10);
    var dm = parseInt(value("sn-daily-max"), 10);
    var um = parseInt(value("sn-urgent-max"), 10);
    var ur = !!(($("sn-urgent") || {}).checked);
    if (t !== snap.type) body.type = t;
    if (secret) body.secret = secret;
    if (!isNaN(cd) && cd !== snap.cooldown) body.cooldown = Math.max(0, cd);
    if (ur !== snap.urgent_only) body.urgent_only = ur;
    if (!isNaN(dm) && dm !== snap.daily_max) body.daily_max = Math.max(0, dm);
    if (!isNaN(um) && um !== snap.urgent_daily_max) body.urgent_daily_max = Math.max(0, um);
    return body;
  }

  function submit(body) {
    return new Promise(function (resolve) {
      YB.openConfirmPasswordModal(
        "保存消息推送配置属于高危操作。\n请输入当前管理员密码确认。",
        function (pw) {
          body.confirm_password = pw;
          busy = true;
          var btn = $("sn-save"); if (btn) btn.disabled = true;
          setTip("保存中…", false);
          YB.api("PUT", "/api/notify-config", body).then(function () {
            var sec = $("sn-secret"); if (sec) sec.value = "";
            resolve(true);
            return load();
          }, function (e) {
            setTip((e && e.message) || "保存失败，请稍后重试", true);
            resolve(false);
          }).then(function () {
            busy = false;
            if (btn && isMaster) btn.disabled = false;
          });
        },
        function () { resolve(false); });     // 取消口令 = 本次不保存
    });
  }

  // 返回 Promise<boolean>：true = 已提交（或本就无改动）；false = 取消或失败。
  function save() {
    if (busy || !isMaster) return Promise.resolve(false);
    var body = collect();
    if (!Object.keys(body).length) {
      clearDirty();
      YB.toast.info("没有需要保存的改动");
      return Promise.resolve(true);
    }
    if (body.type && !body.secret && !snap.configured) { YB.toast.error("开启推送请填写密钥"); return Promise.resolve(false); }
    if (body.secret && body.type === "serverchan" && body.secret.slice(0, 3).toUpperCase() !== "SCT") {
      YB.toast.error("Server酱 SendKey 应以 SCT 开头"); return Promise.resolve(false);
    }
    return submit(body);
  }

  function test() {
    if (busy || !isMaster) return;
    busy = true;
    var btn = $("sn-test"); if (btn) btn.disabled = true;
    setTip("发送中…", false);
    YB.api("POST", "/api/notify-test").then(function (data) {
      setTip((data && data.msg) || "已发送", false);
    }).catch(function (e) {
      setTip((e && e.message) || "发送失败，请稍后重试", true);
    }).then(function () {
      busy = false;
      if (btn && isMaster) btn.disabled = false;
    });
  }

  function mount(options) {
    isMaster = !!(options && options.isMaster);
    var saveBtn = $("sn-save"); if (saveBtn) saveBtn.addEventListener("click", function () { save(); });
    var testBtn = $("sn-test"); if (testBtn) testBtn.addEventListener("click", test);
    ["sn-secret", "sn-cooldown", "sn-daily-max", "sn-urgent-max"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("input", markDirty);
    });
    var urgent = $("sn-urgent");
    if (urgent) urgent.addEventListener("change", markDirty);
    var type = $("sn-type");
    if (type) type.addEventListener("change", markDirty);
    if (!isMaster) {
      // 整卡（推送 + 邮件两段）禁用：容器做 group 并把禁用原因 #sn-perm 关联给读屏
      var card = $("set-notify");
      if (card) {
        disableAll(card, "sn-perm");
        card.setAttribute("role", "group");
        card.setAttribute("aria-describedby", "sn-perm");
      }
      var perm = $("sn-perm"); if (perm) perm.hidden = false;
      var st = $("sn-status"); if (st) st.textContent = "仅主管理员可配置消息推送";
    }
  }

  YB.settingsNotify = {
    mount: mount, load: load, save: save,
    isDirty: function () { return dirty; }
  };
})();
