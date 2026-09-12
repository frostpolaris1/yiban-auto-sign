/* 行内操作下拉（portal 到 document.body + position:fixed）的共享实现。
   挂载到 window.YB.rowMenu；classic script。

   为什么必须 portal：.table-scroll 的 overflow:auto（overflow-x:auto 会让 y 也算 auto）
   与 .collapse-inner 的 overflow:clip 都会裁掉行内绝对定位菜单；祖先的 transform 还会
   让 position:fixed 以该祖先为包含块。故打开时把 .dd-menu 挂到 document.body（无
   transform），关闭时按记录的原父节点还原——用 MutationObserver 观察本 .dd-wrap 的
   is-open：出现即浮起定位，消失即还原（不全局重建，避免行 DOM 漂移）。

   YB.rowMenu.cell(opts) 返回一个已挂好该机制的 <td>：
     opts.items   [{label, icon, danger, run}] —— run 回调不得把完整邮箱/手机号等敏感值
                  写进 DOM 属性；菜单文案里指代用户一律用脱敏值（由调用方保证）
     opts.label   触发器 aria-label / title（默认「更多操作」）
     opts.icon    触发器图标（默认 ellipsis）
     opts.cellClass / opts.btnClass / opts.wrapClass / opts.menuClass 可选
   浮动态沿用既有的 .acct-menu--floating 类名：accounts.js 的轮询守卫在查它，不要改名。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;

  var GAP = 8;                              // 菜单与视口/触发器的安全边距
  var FLOAT_CLASS = "acct-menu--floating";  // 兼容既有轮询守卫，勿改名

  // icon 名只接受白名单形态（小写字母/数字/连字符），不匹配回落 "ellipsis"。
  // 当前调用点都是代码常量，但这是共享组件：一旦后续页面把后端字段当 icon 名传进来，
  // 无校验的 name 会直接拼进 <use href> 形成注入面，故在此收口。
  function svg(name) {
    var n = /^[a-z0-9-]+$/.test(String(name == null ? "" : name)) ? name : "ellipsis";
    return '<svg aria-hidden="true"><use href="#i-' + n + '"/></svg>';
  }

  function watch(wrap) {
    if (!window.MutationObserver) return;
    var menu = wrap.querySelector(".dd-menu");
    if (!menu) return;
    var home = menu.parentNode;   // 原父节点（= wrap），关闭时按此还原
    menu.__ybHome = home;
    var closeTick = 0;            // 每次打开/关闭自增，使在途的延迟关闭回调失效

    function place(trigger) {
      var tr = trigger.getBoundingClientRect();
      var mw = menu.offsetWidth, mh = menu.offsetHeight;
      var vw = window.innerWidth, vh = window.innerHeight;
      // 水平右对齐触发器右缘，再夹进视口（左 >= GAP）
      var left = Math.min(tr.right - mw, vw - mw - GAP);
      if (left < GAP) left = GAP;
      // 垂直默认向下；越界则翻到上方，再夹进视口（上 >= GAP）
      var top = tr.bottom + GAP;
      var up = top + mh > vh - GAP;
      if (up) top = tr.top - mh - GAP;
      if (top < GAP) top = GAP;
      menu.style.left = left + "px";
      menu.style.top = top + "px";
      menu.style.right = "auto";
      menu.style.bottom = "auto";
      // 翻到上方时锚点改右下角，展开方向才与位置一致
      menu.style.transformOrigin = up ? "bottom right" : "top right";
    }

    function floatMenu(trigger) {
      if (!trigger) return;
      closeTick++;   // 重新打开：作废任何在途的延迟关闭回调
      if (menu.parentNode !== document.body) document.body.appendChild(menu);
      menu.classList.add(FLOAT_CLASS);
      place(trigger); // 先定位再显示：fixed + visibility:hidden 下 offsetWidth/Height 已可测
      requestAnimationFrame(function () {
        if (!menu.classList.contains(FLOAT_CLASS)) return;
        menu.classList.add("is-shown");
        // 菜单已 portal 到 body，Tab 不再自然进入；打开时聚焦首项，配合方向键完整键盘操作
        var items = menu.querySelectorAll(".dd-menu-item");
        if (items.length) items[0].focus();
      });
    }

    // 立即拆除（不播动画）：供 closeAll() 的重渲染前清场使用。
    function detachNow() {
      menu.classList.remove("is-shown", FLOAT_CLASS);
      menu.style.left = menu.style.top = menu.style.right = menu.style.bottom = "";
      menu.style.transformOrigin = "";
      if (home && menu.parentNode !== home) home.appendChild(menu);
    }

    // 用户触发的关闭：先只摘 is-shown，等 140ms 退出过渡跑完（transitionend 或 160ms 兜底，
    // 先到者）再摘 FLOAT_CLASS 并还原父节点。同帧摘类会取消退出过渡，浮层「啪」地消失。
    // closeAll() 走 detachNow 的立即拆除路径，两者分工不同、不可互替。
    function restoreMenu() {
      if (!menu.classList.contains(FLOAT_CLASS)) return;
      menu.classList.remove("is-shown");
      var my = ++closeTick;
      function done() {
        if (my !== closeTick) return;   // 已被重新打开，或已被 closeAll 立即拆除
        menu.removeEventListener("transitionend", onEnd);
        detachNow();
      }
      function onEnd(e) { if (e.target === menu) done(); }
      menu.addEventListener("transitionend", onEnd);
      setTimeout(done, 160);
    }

    // core.js 的箭头键导航按 `.dd-wrap.is-open` 查 `.dd-menu-item`；菜单 portal 后查不到
    // （is-open 的 wrap 与 <body> 里的 menu 已分离），故在浮动态内补一份同样的键位处理。
    // Esc 仍由 core.js 的 document 监听关闭 is-open → 观察者还原。
    menu.addEventListener("keydown", function (e) {
      var items = menu.querySelectorAll(".dd-menu-item");
      if (!items.length) return;
      var i = Array.prototype.indexOf.call(items, document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1 + items.length) % items.length].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
      else if (e.key === "Home") { e.preventDefault(); items[0].focus(); }
      else if (e.key === "End") { e.preventDefault(); items[items.length - 1].focus(); }
    });

    new MutationObserver(function () {
      if (wrap.classList.contains("is-open")) floatMenu(wrap.querySelector("[data-dropdown]"));
      else restoreMenu();
    }).observe(wrap, { attributes: true, attributeFilter: ["class"] });
  }

  function cell(opts) {
    opts = opts || {};
    var label = opts.label || "更多操作";
    var wrap = YB.el("div", { class: opts.wrapClass || "dd-wrap acct-row-menu" });
    var trigger = YB.el("button", {
      type: "button", class: opts.btnClass || "btn btn--ghost btn--icon",
      "data-dropdown": "", "aria-haspopup": "menu", "aria-label": label, title: label
    });
    trigger.innerHTML = svg(opts.icon || "ellipsis");
    wrap.appendChild(trigger);

    var menu = YB.el("div", {
      class: "dd-menu" + (opts.menuClass ? " " + opts.menuClass : ""), role: "menu"
    });
    (opts.items || []).forEach(function (it) {
      // 破坏性项前加分隔线（.dd-divider，语义内容分隔），首项不加
      if (it.danger && menu.childNodes.length) menu.appendChild(YB.el("div", { class: "dd-divider" }));
      var item = YB.el("button", {
        type: "button", role: "menuitem",
        class: "dd-menu-item" + (it.danger ? " danger" : "")
      });
      if (it.icon) item.innerHTML = svg(it.icon);   // 常量 SVG 片段，非后端数据
      item.appendChild(YB.el("span", { text: it.label }));
      item.addEventListener("click", function () { if (it.run) it.run(); });
      menu.appendChild(item);
    });
    wrap.appendChild(menu);
    watch(wrap);
    return YB.el("td", { class: opts.cellClass || "" }, [wrap]);
  }

  // 兜底关闭：正常路径由 core.js 摘掉 is-open、观察者负责还原（播放退出过渡）；
  // closeAll() 用于调用方即将重渲染整块行 DOM 时的清场，必须**立即拆除**（不等过渡），
  // 否则行被移出文档后 MutationObserver 已脱附，浮层会残留在 document.body 上。
  function closeAll() {
    [].forEach.call(document.querySelectorAll(".dd-wrap.is-open"), function (w) {
      w.classList.remove("is-open");
    });
    [].forEach.call(document.querySelectorAll("." + FLOAT_CLASS), function (m) {
      m.classList.remove("is-shown", FLOAT_CLASS);
      m.style.left = m.style.top = m.style.right = m.style.bottom = "";
      m.style.transformOrigin = "";
      if (m.__ybHome && m.parentNode !== m.__ybHome) m.__ybHome.appendChild(m);
    });
  }

  YB.rowMenu = { cell: cell, closeAll: closeAll };
})();
