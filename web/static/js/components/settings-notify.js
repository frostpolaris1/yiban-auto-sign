/* 系统设置 · 消息推送卡（管理端 /settings）。
   挂载到 window.YB.settingsNotify；classic script。邮件通知卡拆在 settings-mail.js。

   权限：配置与测试均仅主管理员；关闭通道、更换/清空密钥、调整额度节流都需
   confirm_password（后端 _high_risk_gate，前端先收口令再提交；UI 不是安全边界）。
   脱敏：密钥只读展示 secret_masked，输入框恒为空（留空=不改动），绝不回显。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var isMaster = false;
  var snap = null;
  var busy = false;

  function $(id) { return document.getElementById(id); }
  function setTip(text, bad) {
    var el = $("sn-tip");
    if (!el) return;
    el.textContent = text || "";
    el.className = bad ? "set-tip set-bad" : "set-tip";
  }
  function disableAll(root) {
    if (!root) return;
    [].forEach.call(root.querySelectorAll("input,select,button,textarea"), function (n) { n.disabled = true; });
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
    var g = data.daily_remaining == null ? "不限" : data.daily_remaining + " 条";
    var u = data.urgent_daily_remaining == null ? "不限" : data.urgent_daily_remaining + " 条";
    parts.push("今日额度：非紧急剩余 " + g + " / 紧急剩余 " + u);
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
      var t = $("sn-type"); if (t) t.value = snap.type;
      var sec = $("sn-secret"); if (sec) sec.value = "";
      var cd = $("sn-cooldown"); if (cd && snap.cooldown != null) cd.value = String(snap.cooldown);
      var ur = $("sn-urgent"); if (ur) ur.checked = snap.urgent_only;
      var dm = $("sn-daily-max"); if (dm && snap.daily_max != null) dm.value = String(snap.daily_max);
      var um = $("sn-urgent-max"); if (um && snap.urgent_daily_max != null) um.value = String(snap.urgent_daily_max);
      renderStatus(data);
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

  function save() {
    if (busy) return;
    var body = collect();
    if (!Object.keys(body).length) { YB.toast.info("没有需要保存的改动"); return; }
    if (body.type && !body.secret && !snap.configured) { YB.toast.error("开启推送请填写密钥"); return; }
    if (body.secret && body.type === "serverchan" && body.secret.slice(0, 3).toUpperCase() !== "SCT") {
      YB.toast.error("Server酱 SendKey 应以 SCT 开头"); return;
    }
    YB.openConfirmPasswordModal(
      "保存消息推送配置属于高危操作（关闭通道 / 更换密钥 / 调整额度节流）。\n请输入当前管理员密码确认。",
      function (pw) {
        body.confirm_password = pw;
        busy = true;
        var btn = $("sn-save"); if (btn) btn.disabled = true;
        YB.api("PUT", "/api/notify-config", body).then(function () {
          setTip("已保存", false);
          var sec = $("sn-secret"); if (sec) sec.value = "";
          return load();
        }).catch(function (e) {
          setTip((e && e.message) || "保存失败，请稍后重试", true);
        }).then(function () {
          busy = false;
          if (btn) btn.disabled = false;
        });
      });
  }

  function test() {
    if (busy) return;
    busy = true;
    var btn = $("sn-test"); if (btn) btn.disabled = true;
    setTip("发送中…", false);
    YB.api("POST", "/api/notify-test").then(function (data) {
      setTip((data && data.msg) || "已发送", false);
    }).catch(function (e) {
      setTip((e && e.message) || "发送失败，请稍后重试", true);
    }).then(function () {
      busy = false;
      if (btn) btn.disabled = false;
    });
  }

  function mount(options) {
    isMaster = !!(options && options.isMaster);
    var saveBtn = $("sn-save"); if (saveBtn) saveBtn.addEventListener("click", save);
    var testBtn = $("sn-test"); if (testBtn) testBtn.addEventListener("click", test);
    if (!isMaster) {
      var card = $("set-notify");
      if (card) {
        // 整卡控件禁用：容器做 group 并把禁用原因 #sn-perm 关联给读屏
        disableAll(card);
        card.setAttribute("role", "group");
        card.setAttribute("aria-describedby", "sn-perm");
      }
      var perm = $("sn-perm"); if (perm) perm.hidden = false;
      var st = $("sn-status"); if (st) st.textContent = "仅主管理员可配置消息推送";
    }
  }

  YB.settingsNotify = { mount: mount, load: load };
})();
