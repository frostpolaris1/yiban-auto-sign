/* 「我的账号」卡片列表（用户端与管理端 /mine 共用的唯一实现）。
   挂载到 window.YB.myAccounts；classic script，公开面 mount(opts) → { reload, accounts }。

   opts：
     listSel        账号卡片容器 id（如 "account-list"）
     emptySel       空态 <p class="empty"> id（可空）
     openBtnSel     「提交我的易班账号」按钮 id（可空）；有未删除账号时隐藏
     formVariant    传给 YB.accountForm 的 variant（"user"）
     calendarHref   生效账号卡片的「签到日历」链接目标；null/缺省则不出该链接
     calendarMode   "link"（默认，出链接）| "inline"（卡内挂 [data-sc-mount] 并调共享日历）
     showState      是否在生效账号卡上补一行今日状态（今日已完成签到 / 前方排队 N 人）。
                    管理端 /mine 需要该信息（旧页在卡片内联展示），用户端不显示。
     deletedRowsInList  列表视图是否包含软删除行（普通用户 true，管理端 false）。
                    决定删除成功后原地替换该卡还是移除并重排后续下标。
     onChanged      任一写操作成功后回调（可空）

   接口契约（/api/my-accounts + 账号表单）与 pages/user_account.js 原实现逐字等价。
   手机号沿用列表下发值（后端按登录身份作用域下发），动态文本一律 textContent / YB.el。 */
(function () {
  "use strict";
  var YB = window.YB;
  if (!YB) return;
  var $ = YB.$;

  // 账号卡图标表达**审核状态**；常量名白名单，缺失回落 clock
  var AUDIT_ICON = { pending: "clock", rejected: "circle-x", active: "circle-check" };

  function svg(name) {
    return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  }

  // 语义徽标：用全站达标的 .badge--* 档位（vendor 的 .badge.success 等不达 AA）
  function badge(text, tone) {
    return YB.el("span", { class: "badge" + (tone ? " badge--" + tone : ""), text: text });
  }

  function statusBadge(status) {
    if (status === "pending") return badge("待审核", "warn");
    if (status === "rejected") return badge("已拒绝", "bad");
    if (status === "active") return badge("已生效", "ok");
    return badge(String(status == null ? "" : status));
  }

  function auditAriaText(a) {
    if (a.deleted) return a.deleted_by_me ? "状态：已删除（7 天内可撤销）" : "状态：已被管理员删除";
    if (a.user_paused) return "状态：已取消（可恢复签到）";
    var map = { pending: "待审核", rejected: "已拒绝", active: "已生效" };
    return "状态：" + (map[a.status] || "未知");
  }

  function actionButton(label, cls, onClick) {
    var b = YB.el("button", { type: "button", class: cls, text: label });
    // 把按钮本身交给处理函数：成功/失败时可就地置 disabled + 文案，无需查 DOM
    b.addEventListener("click", function () { onClick(b); });
    return b;
  }

  // 写操作成功后的就地状态行：不整表重写，只往**本卡**追加一行，
  // 数秒后淡出移除（reduced-motion 由第 14 节统一取消过渡，仍在定时后移除），
  // 避免长期与卡片徽章重复。动态文本只走 text。
  var FLASH_MS = 4000;
  function flashLine(card, text) {
    var line = YB.el("p", { class: "state-line state-line--ok state-line--flash", role: "status", text: text });
    card.appendChild(line);
    setTimeout(function () {
      line.classList.add("is-fading");
      setTimeout(function () { if (line.parentNode) line.parentNode.removeChild(line); }, 300);
    }, FLASH_MS);
  }

  function mount(opts) {
    opts = opts || {};
    var listEl = $(opts.listSel);
    if (!listEl) return { reload: function () {}, accounts: [] };
    var emptyEl = opts.emptySel ? $(opts.emptySel) : null;
    var openBtn = opts.openBtnSel ? $(opts.openBtnSel) : null;
    var formVariant = opts.formVariant || "user";
    var calendarHref = opts.calendarHref || null;
    var inline = opts.calendarMode === "inline";
    var showState = !!opts.showState;
    var onChanged = typeof opts.onChanged === "function" ? opts.onChanged : null;

    var accounts = [];
    var opBusy = false;   // 暂停/恢复、删除、撤销删除共用的在途守卫（防连点/双发）
    // 普通用户视图含软删除行（撤销入口留在卡内）；管理端视图按后端口径排除已删除行
    var keepDeleted = opts.deletedRowsInList !== false;

    function notifyChanged() { if (onChanged) onChanged(); }

    // 失败态借用空态位展示错误文案 + 重试入口，避免「只弹一个 toast、列表区留白」
    // 被误读成没有账号（对齐 work_users 的状态条口径）。文案初值在 mount 时缓存，
    // 恢复成功后还原，防止下次真正的空列表沿用错误文案。
    var emptyText0 = emptyEl && emptyEl.querySelector(".empty__msg")
      ? emptyEl.querySelector(".empty__msg").textContent : "";
    function showLoadFailure(msg) {
      if (!emptyEl) return;
      var node = emptyEl.querySelector(".empty__msg");
      if (node) node.textContent = msg || "账号列表加载失败，请检查网络后重试";
      var rb = emptyEl.querySelector("[data-acct-retry]");
      if (rb) rb.hidden = false;
      emptyEl.hidden = false;
    }
    function clearLoadFailure() {
      if (!emptyEl) return;
      var node = emptyEl.querySelector(".empty__msg");
      if (node) node.textContent = emptyText0;
      var rb = emptyEl.querySelector("[data-acct-retry]");
      if (rb) rb.hidden = true;
    }

    function loadAccounts() {
      return YB.api("GET", "/api/my-accounts").then(function (data) {
        accounts = (data && data.accounts) || [];
        clearLoadFailure();
        renderList();
      }).catch(function (e) {
        YB.toast.error(e.message);
        showLoadFailure();
      });
    }

    function actionLink(label, cls, href) {
      return YB.el("a", { class: cls, href: href, text: label });
    }

    function accountCard(a, i) {
      var card = YB.el("div", { class: "account-card" });
      var head = YB.el("div", { class: "account-head" });
      var ident = YB.el("div", { class: "account-ident" });

      var iconBox = YB.el("span", { class: "account-icon" });
      iconBox.innerHTML = svg(a.deleted ? "trash" : (AUDIT_ICON[a.status] || "clock"));
      ident.appendChild(iconBox);
      ident.appendChild(YB.el("span", { class: "sr-only", text: auditAriaText(a) }));

      var info = YB.el("div");
      // 名称 + 审核徽章同一行：徽章不占操作行，竖屏按钮才排得下
      var titleRow = YB.el("div", { class: "account-title-row" });
      titleRow.appendChild(YB.el("span", { class: "account-name", text: a.display_name }));
      if (a.deleted) titleRow.appendChild(badge("已删除"));
      else if (a.user_paused) titleRow.appendChild(badge("已取消", "bad"));
      else titleRow.appendChild(statusBadge(a.status));
      info.appendChild(titleRow);
      info.appendChild(YB.el("div", {
        class: "account-meta",
        text: String(a.phone || "") + (a.phone_model ? " · " + a.phone_model : "")
      }));
      // 今日状态：仅管理端要求（旧 /mine 在卡片内展示排队数/完成态）；用户端由日历页承担
      if (showState && !a.deleted && a.status === "active" && a.state_status !== "paused") {
        var done = a.state_status === "success" || a.state_status === "already";
        var stateText = done ? "今日已完成签到"
          : (a.queue_ahead != null ? "前方排队 " + a.queue_ahead + " 人" : "");
        if (stateText) info.appendChild(YB.el("div", { class: "account-note", text: stateText }));
      }
      if (a.deleted) {
        info.appendChild(YB.el("div", {
          class: "account-note",
          text: a.deleted_by_me
            ? "你已删除此账号，7 天内可撤销恢复，超期自动清除"
            : "已被管理员删除，待管理员处理"
        }));
      }
      ident.appendChild(info);

      var actions = YB.el("div", { class: "account-actions" });
      if (a.deleted) {
        if (a.deleted_by_me) {
          actions.appendChild(actionButton("撤销删除", "btn btn--ghost btn--sm", function (btn) { restoreAccount(i, btn); }));
        } else {
          actions.appendChild(YB.el("span", { class: "account-note", text: "待管理员处理" }));
        }
      } else {
        if (a.status === "active") {
          if (!inline && calendarHref) {
            actions.appendChild(actionLink("签到日历", "btn btn--ghost btn--sm", calendarHref));
          }
          // pause_forbidden 由后端按账号归属下发（管理员直属账号为 true），不写死角色判断
          if (!a.pause_forbidden) {
            actions.appendChild(actionButton(
              a.user_paused ? "恢复签到" : "暂停签到",
              "btn btn--ghost btn--sm",
              function (btn) { togglePause(i, btn); }
            ));
          }
        }
        actions.appendChild(actionButton(
          a.status === "rejected" ? "修改并重新提交" : "编辑",
          "btn btn--ghost btn--sm",
          function () { openAccountForm(i); }
        ));
        actions.appendChild(actionButton("删除", "btn btn--ghost btn--danger-ghost btn--sm", function (btn) { deleteAccount(i, btn); }));
      }
      head.appendChild(ident);
      head.appendChild(actions);
      card.appendChild(head);
      // 写操作成功后的就地状态：不整表重写，故用一次性标记把结果留在原卡内；
      // 读完即清，后续因其它卡重排而重建本卡时不会重复出线
      if (a._flash) { flashLine(card, a._flash); a._flash = null; }

      // 只保留"需要用户本人处理"的异常提示（例行签到状态在「签到日历」）
      if (a.status === "rejected") {
        var rej = YB.el("div", { class: "alert danger account-reject", role: "status" });
        rej.appendChild(YB.el("span", { class: "ico", html: svg("circle-alert") }));
        rej.appendChild(YB.el("span", {
          class: "body",
          text: "账号已被拒绝" + (a.reject_reason ? "：" + a.reject_reason : "") + "。修改后点「修改并重新提交」。"
        }));
        card.appendChild(rej);
      }
      if (!a.deleted && a.status === "active" && a.state_status === "paused") {
        var bad = YB.el("div", { class: "alert danger account-reject", role: "status" });
        bad.appendChild(YB.el("span", { class: "ico", html: svg("circle-alert") }));
        bad.appendChild(YB.el("span", { class: "body", text: "账号密码异常，签到已暂停，请编辑账号更新密码。" }));
        card.appendChild(bad);
      }
      if (a.logs && a.logs.length) {
        var det = YB.el("details", { class: "account-details" });
        det.appendChild(YB.el("summary", { text: "最近签到记录（" + a.logs.length + " 条）" }));
        det.appendChild(YB.el("pre", { class: "log-view", text: a.logs.join(String.fromCharCode(10)) }));
        card.appendChild(det);
      }
      if (!a.deleted && a.status === "pending") {
        card.appendChild(YB.el("p", { class: "account-pending-hint", text: "审核通过后自动签到，结果见「签到日历」。" }));
      }
      // 内联日历：仅生效账号，挂载点置卡片尾部，由 renderList 在入 DOM 后渲染
      if (inline && !a.deleted && a.status === "active") {
        var mountBox = YB.el("div", { class: "sc-mount" });
        mountBox.setAttribute("data-sc-mount", "");
        card.appendChild(mountBox);
      }
      return card;
    }

    // 外壳显隐（空态 / 提交入口）与卡片内容解耦：局部更新时只同步这两处
    function syncChrome() {
      if (emptyEl) emptyEl.hidden = accounts.length > 0;
      // 还有未删除账号时隐藏入口；全部被删除时保留（软删除不死路）
      if (openBtn) openBtn.hidden = accounts.some(function (a) { return !a.deleted; });
    }

    function mountInline(node, a) {
      if (!inline || !window.SignCalendar || a.deleted || a.status !== "active") return;
      var box = node && node.querySelector("[data-sc-mount]");
      if (box) window.SignCalendar.render(box, a.phone);
    }

    function renderList() {
      listEl.innerHTML = "";
      syncChrome();
      accounts.forEach(function (a, i) {
        listEl.appendChild(accountCard(a, i));
      });
      if (inline) accounts.forEach(function (a, i) { mountInline(listEl.children[i], a); });
    }

    // 原地替换单张卡：不整表重写，滚动位置与其它卡的键盘焦点不受影响
    function replaceCard(i) {
      var old = listEl.children[i];
      var next = accountCard(accounts[i], i);
      if (old && old.parentNode === listEl) listEl.replaceChild(next, old);
      else listEl.appendChild(next);
      syncChrome();
      mountInline(next, accounts[i]);
      // 操作按钮已被替换/禁用，把焦点交回新卡首个可操作元素（键盘用户不丢位）
      var f = next.querySelector("button:not([disabled]), a[href]");
      if (f && typeof f.focus === "function") f.focus();
    }

    // 删除后该行离开视图（管理端视图排除软删除行）：移除卡片并重排后续下标
    function removeCard(i) {
      accounts.splice(i, 1);
      var old = listEl.children[i];
      if (old && old.parentNode === listEl) listEl.removeChild(old);
      for (var j = i; j < accounts.length; j++) {
        var node = listEl.children[j];
        var next = accountCard(accounts[j], j);
        if (node && node.parentNode === listEl) listEl.replaceChild(next, node);
        else listEl.appendChild(next);
        mountInline(next, accounts[j]);
      }
      syncChrome();
      var f = listEl.querySelector("button:not([disabled]), a[href]");
      if (f && typeof f.focus === "function") f.focus();
    }

    function setRowBusy(btn, label) {
      if (!btn) return;
      btn.disabled = true;
      btn.textContent = label || "处理中…";
      btn.setAttribute("aria-busy", "true");
    }

    function togglePause(i, btn) {
      if (opBusy) return;
      opBusy = true;
      var a = accounts[i];
      var next = !a.user_paused;
      var busyLabel = a.user_paused ? "恢复签到" : "暂停签到";
      var ask = next
        ? YB.confirmDialog({
            title: "暂停签到",
            body: "确定暂停「" + a.display_name + "」的签到吗？暂停后系统将不再自动签到，可随时恢复。",
            confirmText: "暂停签到", danger: true
          })
        : Promise.resolve(true);
      ask.then(function (ok) {
        if (!ok) { opBusy = false; return; }
        setRowBusy(btn);
        YB.api("PUT", "/api/my-accounts/" + i + "/pause", { paused: next }).then(function (data) {
          YB.toast.success(data.msg || (next ? "已暂停" : "已恢复"));
          a.user_paused = (data && typeof data.paused === "boolean") ? data.paused : next;
          // 就地反馈与删除同款：本卡内追加状态行，按卡替换而非整表重排
          a._flash = a.user_paused ? "✓ 已暂停签到" : "✓ 已恢复签到";
          notifyChanged();
          replaceCard(i);
        }).catch(function (e) {
          YB.toast.error(e.message);
          if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = busyLabel; btn.removeAttribute("aria-busy"); }
        }).then(function () { opBusy = false; });
      });
    }

    function deleteAccount(i, btn) {
      if (opBusy) return;
      opBusy = true;
      var a = accounts[i];
      YB.confirmDialog({
        title: "删除账号",
        body: "确定删除「" + a.display_name + "」(" + a.phone + ") 吗？删除后 7 天内可撤销恢复，超过 7 天将自动清除。",
        confirmText: "删除", danger: true
      }).then(function (ok) {
        if (!ok) { opBusy = false; return; }
        setRowBusy(btn);
        YB.api("DELETE", "/api/my-accounts/" + i).then(function (data) {
          // 视图含软删除行：后端仍保留该行，故只改本地字段并原地替换
          // 视图不含（管理端）：该行已离开列表，移除并重排
          YB.toast.success(keepDeleted
            ? ((data && data.msg) || "已删除，7 天内可撤销")
            : "已删除，可在账号管理页恢复");
          a.deleted = true;
          a.deleted_by_me = true;
          a._flash = "✓ 已删除，7 天内可撤销";
          notifyChanged();
          if (keepDeleted) replaceCard(i);
          else removeCard(i);
        }).catch(function (e) {
          YB.toast.error(e.message);
          if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = "删除"; btn.removeAttribute("aria-busy"); }
        }).then(function () { opBusy = false; });
      });
    }

    function restoreAccount(i, btn) {
      if (opBusy) return;
      opBusy = true;
      var a = accounts[i];
      YB.confirmDialog({
        title: "撤销删除",
        body: "撤销删除「" + a.display_name + "」(" + a.phone + ")？将恢复到删除前的状态。",
        confirmText: "撤销删除"
      }).then(function (ok) {
        if (!ok) { opBusy = false; return; }
        setRowBusy(btn);
        YB.api("POST", "/api/my-accounts/" + i + "/restore", {}).then(function (data) {
          YB.toast.success((data && data.msg) || "已恢复");
          // 接口同时回传刷新后的视图：按手机号对回本行，字段与原状态一致
          var fresh = data && data.accounts;
          var match = fresh && fresh.filter(function (x) { return x.phone === a.phone; })[0];
          if (match) {
            Object.keys(match).forEach(function (k) { a[k] = match[k]; });
          } else {
            a.deleted = false;
            a.deleted_by_me = false;
          }
          a._flash = "✓ 已恢复账号";
          notifyChanged();
          replaceCard(i);
        }).catch(function (e) {
          YB.toast.error(e.message);
          if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = "撤销删除"; btn.removeAttribute("aria-busy"); }
        }).then(function () { opBusy = false; });
      });
    }

    /* 提交 / 编辑：差异只在 variant；账号表单是共享唯一实现 */
    function openAccountForm(index) {
      var editing = typeof index === "number";
      YB.accountForm.open({
        variant: formVariant,
        index: editing ? index : null,
        account: editing ? accounts[index] : null,
        endpoints: { create: "/api/my-accounts", update: "/api/my-accounts/" },
        lockButton: false,   // 沿用原实现：仅用在途标志防连点，不改按钮
        onSaved: function () { notifyChanged(); loadAccounts(); }
      });
    }

    if (openBtn) {
      openBtn.addEventListener("click", function () { openAccountForm(); });
    }

    // 加载失败错误态的重试入口（空态位内，见 showLoadFailure/clearLoadFailure）
    if (emptyEl) {
      var rb = emptyEl.querySelector("[data-acct-retry]");
      if (rb) rb.addEventListener("click", function () { loadAccounts(); });
    }

    return {
      reload: loadAccounts,
      get accounts() { return accounts; }
    };
  }

  YB.myAccounts = { mount: mount };
})();
