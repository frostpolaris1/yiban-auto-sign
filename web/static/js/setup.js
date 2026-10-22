/* 首次启动引导页（/setup）的表单提交。外置脚本：CSP 不放行内联脚本。
   服务端是唯一判据：客户端只做两次口令一致性与空值提示，账号格式与口令策略
   由 /api/setup/admin 复核。 */
(function () {
  "use strict";

  var form = document.getElementById("setup-form");
  if (!form) return;
  var errorBox = document.getElementById("setup-error");
  var button = document.getElementById("setup-btn");
  var base = form.getAttribute("data-base") || "";

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var username = document.getElementById("setup-username").value.trim();
    var password = document.getElementById("setup-password").value;
    var password2 = document.getElementById("setup-password2").value;

    if (!username) {
      showError("请填写管理员账号。");
      return;
    }
    if (!password) {
      showError("请填写口令。");
      return;
    }
    if (password !== password2) {
      showError("两次输入的密码不一致。");
      return;
    }

    errorBox.hidden = true;
    button.disabled = true;
    fetch(base + "/api/setup/admin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ username: username, password: password }),
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) {
            showError(data.error || "创建失败，请重试。");
            button.disabled = false;
            return;
          }
          window.location.assign(base + "/");
        });
      })
      .catch(function () {
        showError("网络异常，请重试。");
        button.disabled = false;
      });
  });
})();
