/* 自选签到时段网格（5 分钟粒度，用户端与「我的账号」共用的唯一实现）。
   挂载到 window.YB.timePref；classic script，公开面 YB.timePref.mount(opts) → 控制器。

   接口：GET /api/my-time-pref 读，PUT /api/my-time-pref 写（slot_min 整数 / null 清除）。
   四态（由后端 slots 字段驱动）：禁用（完全落入掐头去尾裁剪区）/ 满员（pct>=100，
   仍可选、先到先得）/ 部分裁剪（edge_note，虚线框提示）/ 常规；选中态再叠加。

   容器 id 通过 opts.ids 配置，默认即用户端 pages/user_accounts.html 的取值；
   换页面复用只需换一组 id。所有动态文本走 textContent，不拼 innerHTML。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;

  var DEFAULT_IDS = {
    card: "time-pref-card",
    window: "pref-window",
    grid: "pref-slot-grid",
    estimate: "pref-estimate",
    disabledHint: "pref-disabled-hint",
    clearBtn: "pref-clear-btn",
    collapseBtn: "pref-collapse-btn",
    collapseLabel: "pref-collapse-label",
    body: "pref-body",
    tip: "pref-tip"
  };

  function mount(opts) {
    opts = opts || {};
    var ids = Object.assign({}, DEFAULT_IDS, opts.ids || {});
    var getPath = opts.getPath || "/api/my-time-pref";
    var putPath = opts.putPath || "/api/my-time-pref";
    var collapsed = false;
    var bound = false;

    function node(key) { return $(ids[key]); }

    // 折叠区：button[aria-expanded] + .collapse-body.is-open，高度动画由 CSS 的
    // grid-template-rows 0fr↔1fr 完成（开与关都有动画）。
    function setCollapsed(next) {
      collapsed = !!next;
      var btn = node("collapseBtn"), body = node("body");
      if (!btn || !body) return;
      btn.setAttribute("aria-expanded", String(!collapsed));
      body.classList.toggle("is-open", !collapsed);
      var label = node("collapseLabel");
      if (label) label.textContent = collapsed ? "展开配置" : "收起";
    }

    function renderSlots(data) {
      var grid = node("grid");
      if (!grid) return;
      grid.innerHTML = "";
      var tip = "";
      var slots = data.slots || [];
      slots.forEach(function (s, i) {
        var btn = YB.el("button", { type: "button" });
        if (s.disabled) {
          // 完全落入掐头去尾裁剪区：不可选
          btn.className = "slot slot--off";
          btn.disabled = true;
          btn.title = "该时段被掐头去尾保留，不可选择";
          btn.appendChild(YB.el("div", { class: "slot-name", text: s.label }));
          btn.appendChild(YB.el("div", { class: "slot-pct", text: "已保留" }));
          grid.appendChild(btn);
          return;
        }
        var sel = data.pref_slot === s.slot_min;
        var full = s.pct >= 100;          // 满员仍可选（先到先得 + 溢出顺延），用警示色提示
        var partial = !!s.edge_note;      // 部分落入裁剪区：虚线框，调度在可用部分执行
        btn.className = "slot" + (sel ? " slot--on" : full ? " slot--full" : partial ? " slot--partial" : "");
        if (partial) btn.title = s.edge_note + "，选中后将在可用部分为你签到";
        btn.appendChild(YB.el("div", { class: "slot-name", text: s.label }));
        btn.appendChild(YB.el("div", { class: "slot-pct", text: "已选" + s.pct + "%" }));
        btn.addEventListener("click", function () { pick(s.slot_min); });
        grid.appendChild(btn);
        // 首尾时段提醒（选中时）；部分裁剪提示优先，未开启时与「暂不生效」拼接
        if (sel && (i === 0 || i === slots.length - 1)) {
          var edgeTip = s.edge_note ? s.edge_note + "，选中后将在可用部分签到"
            : i === 0 ? "最早时段：窗口开始后最先为你签到"
              : "最后时段：临近窗口截止执行，网络波动可能导致错过";
          tip = (data.allowed ? "" : "未开启：") + edgeTip;
        }
      });
      var tipEl = node("tip");
      if (tipEl) {
        tipEl.textContent = tip;
        tipEl.classList.toggle("state-line--warn", !!tip);
      }
    }

    // preserve=true 时保留用户当前展开态，用于选择后的局部刷新，避免页面跳动
    function load(preserve) {
      var card = node("card");
      return YB.api("GET", getPath).then(function (data) {
        if (!data.has_account) { if (card) card.hidden = true; return; }
        if (card) card.hidden = false;
        var win = node("window");
        if (win) win.textContent = data.window || "";
        renderSlots(data);
        var hint = node("disabledHint");
        if (hint) hint.hidden = !!data.allowed;
        var est = node("estimate");
        if (est) {
          if (data.allowed && data.pref) {
            est.textContent = "";
            est.hidden = true;
          } else if (data.estimated) {
            est.textContent = "预计签到时段：" + data.estimated + (data.estimate_note || "")
              + (data.allowed ? "" : "（自选未开启，按自动分配）");
            est.hidden = false;
          } else {
            est.textContent = data.estimate_note || "";
            est.hidden = !data.estimate_note;
          }
        }
        // 未开启时默认收起；修改后的局部刷新保留用户当前展开态
        if (!preserve) setCollapsed(!data.allowed);
      }).catch(function () { if (card) card.hidden = true; });
    }

    function pick(slot) {
      YB.api("PUT", putPath, { slot_min: slot }).then(function (data) {
        YB.toast.success(data.msg || "已保存");
        load(true);
      }).catch(function (e) { YB.toast.error(e.message); });
    }

    function clear() {
      YB.api("PUT", putPath, { slot_min: null }).then(function (data) {
        YB.toast.success(data.msg || "已清除");
        load(true);
      }).catch(function (e) { YB.toast.error(e.message); });
    }

    function bind() {
      if (bound) return;
      bound = true;
      var collapseBtn = node("collapseBtn");
      if (collapseBtn) collapseBtn.addEventListener("click", function () { setCollapsed(!collapsed); });
      var clearBtn = node("clearBtn");
      if (clearBtn) clearBtn.addEventListener("click", clear);
    }

    bind();
    return { load: load, setCollapsed: setCollapsed, ids: ids };
  }

  YB.timePref = { mount: mount };
})();
