// 易班自动签到 · 管理端脚本 —— 用户管理（待审核 / 正式 / 空置 + 已删除用户）
// 本文件是 web/static/js/app.js 的**连续区间** L2094-L2359，内容一字未改（仅加这 3 行头）。
// classic script、共享全局作用域：加载顺序见 templates/index.html，**顺序不可随意调整**。
async function loadUsers() {
  try {
    const data = await api('/api/users');
    allUsers = data.users || [];
    builtinAdminName = data.builtin_admin || 'admin';
    renderUsers(allUsers, builtinAdminName);
  } catch (e) { toast(e.message, true); }
  loadDeletedUsers();  // v0.20.1：已注销用户冷却视图（失败静默，非关键）
}

// 已注销用户（软删除 7 天冷却期；天粒度剩余时间，v0.20.1）
let deletedUsers = [];
let deletedUsersCollapsed = true;  // 默认收起，不占空间

async function loadDeletedUsers() {
  try {
    const data = await api('/api/users/deleted');
    deletedUsers = data.items || [];
    renderDeletedUsers();
  } catch (e) { /* 静默：视图非关键 */ }
}

function toggleDeletedUsers() {
  deletedUsersCollapsed = !deletedUsersCollapsed;
  $('deleted-users-body').classList.toggle('hidden', deletedUsersCollapsed);
  $('deleted-arrow').innerHTML = deletedUsersCollapsed ? icon('chevR') : icon('chevD');
}

function renderDeletedUsers() {
  const card = $('deleted-users-card');
  card.classList.toggle('hidden', deletedUsers.length === 0);  // 无注销用户整卡隐藏
  if (!deletedUsers.length) return;
  $('users-deleted-count').textContent = `（${deletedUsers.length} 人）`;
  $('deleted-users-body').classList.toggle('hidden', deletedUsersCollapsed);
  $('deleted-arrow').innerHTML = deletedUsersCollapsed ? icon('chevR') : icon('chevD');
  const tbody = $('users-deleted-tbody');
  tbody.innerHTML = '';
  deletedUsers.forEach(u => {
    const tr = document.createElement('tr');
    tr.className = 'border-b border-zinc-50 dark:border-zinc-700/50 hover:bg-zinc-50 dark:bg-zinc-800 dark:hover:bg-zinc-700/50';
    // 剩余时间：冷却中 ≥1 天 → "剩余 X 天"；不足 1 天 → "不足一天"；待清除 → "—"
    const remainText = u.status === 'purge_pending' ? '—'
      : (u.remaining_days >= 1 ? `剩余 ${u.remaining_days} 天` : '不足一天');
    const statusText = u.status === 'purge_pending' ? '待清除' : '冷却中';
    const statusCls = u.status === 'purge_pending'
      ? 'text-red-600 dark:text-red-400' : 'text-amber-600 dark:text-amber-400';
    tr.innerHTML = `
      <td class="px-4 py-3 font-mono text-sm text-zinc-900 dark:text-zinc-100 whitespace-nowrap truncate" title="${esc(u.email)}">${esc(u.email)}</td>
      <td class="px-4 py-3 text-zinc-500 dark:text-zinc-400 whitespace-nowrap">${esc(u.deleted_at || '—')}</td>
      <td class="px-4 py-3 text-zinc-500 dark:text-zinc-400 whitespace-nowrap">${esc(remainText)}</td>
      <td class="px-4 py-3 text-sm whitespace-nowrap"><span class="${statusCls}">${esc(statusText)}</span></td>
      <td class="px-4 py-3 text-sm whitespace-nowrap">
        ${state.isMasterAdmin
          ? `<button data-purge-email="${esc(u.email)}"
                class="text-xs text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 transition-colors duration-150">立即清除</button>`
          : `<span class="text-xs text-zinc-500 dark:text-zinc-400">仅主管理员可清除</span>`}
      </td>`;
    tbody.appendChild(tr);
  });
}

// 管理员立即清除已注销用户（2026-08-17：物理删除用户+易班账号+自选时间，不可恢复）
// 2026-08-29 高危操作二次鉴权：须输入当前管理员密码确认
async function purgeDeletedUser(email) {
  openConfirmPasswordModal(`立即彻底清除用户 ${email}？\n将物理删除其注册信息、易班账号与自选签到时间，此操作不可恢复！\n请输入当前管理员密码确认。`, async (pw) => {
    try {
      const data = await api('/api/users/deleted/purge', {
        method: 'POST', body: JSON.stringify({emails: [email], confirm_password: pw})
      });
      toast(data.msg || '已清除');
      loadDeletedUsers();
    } catch (e) { /* api() 已 toast 错误 */ }
  });
}

function renderUsers(users, builtinAdmin) {
  // 批量开关：控制表头复选框列显示
  document.querySelectorAll('.batch-col').forEach(th => th.classList.toggle('hidden', !state.batchMode));
  document.querySelectorAll('[id^="batch-bar-"]').forEach(bar => {
    // 待删除组折叠时批量条保持隐藏（即使批量模式开启）
    const collapsed = bar.id === 'batch-bar-deleted' && state.deletedCollapsed;
    bar.classList.toggle('hidden', !state.batchMode || collapsed);
  });
  // review 口径 = 待审核 + 已拒绝（与账号管理「待处理账号」组一致；v0.29.0 修复）
  const pendingUsers = users.filter(u => (u.review_count || 0) > 0);
  const normalUsers = users.filter(u => !((u.review_count || 0) > 0) && (u.account_count || 0) > 0);
  const emptyUsers = users.filter(u => !((u.review_count || 0) > 0) && (u.account_count || 0) === 0);
  const pendingFiltered = pendingUsers.filter(u => userMatch(u, usersPendingSearch));
  const normalFiltered = normalUsers.filter(u => userMatch(u, usersNormalSearch));
  const emptyFiltered = emptyUsers.filter(u => userMatch(u, usersVacantSearch));
  // 第一组：待审核用户
  const ptbody = $('users-pending-tbody');
  ptbody.innerHTML = '';
  $('users-pending-empty').classList.toggle('hidden', pendingFiltered.length > 0);
  $('users-pending-empty').textContent = pendingFiltered.length ? '' : (pendingUsers.length ? '无匹配结果' : '暂无待处理用户');
  $('users-pending-count').textContent = usersPendingSearch
    ? `（${pendingFiltered.length} 人匹配 / 共 ${pendingUsers.length} 人待处理）`
    : (pendingUsers.length ? `（${pendingUsers.length} 人待处理）` : '');
  pendingFiltered.forEach(u => ptbody.appendChild(userRow(u, 'usersPending')));
  // 第二组：正式用户（含内置管理员）
  const tbody = $('users-tbody');
  tbody.innerHTML = '';
  $('users-empty').classList.toggle('hidden', normalFiltered.length > 0);
  $('users-empty').textContent = normalFiltered.length ? '' : (normalUsers.length ? '无匹配结果' : '暂无注册用户');
  $('users-normal-count').textContent = usersNormalSearch
    ? `（${normalFiltered.length} 人匹配 / 共 ${normalUsers.length} 人）`
    : (normalUsers.length ? `（${normalUsers.length} 人）` : '');
  const builtin = document.createElement('tr');
  builtin.className = 'border-b border-zinc-50 dark:border-zinc-700/50 bg-zinc-50/50 dark:bg-zinc-700/30';
  builtin.innerHTML = `
    ${state.batchMode ? '<td class="px-4 py-3 w-12"></td>' : ''}
    <td class="px-4 py-3 font-mono text-sm text-zinc-900 dark:text-zinc-100 whitespace-nowrap max-w-[220px] truncate" title="${esc(builtinAdmin)}">${esc(builtinAdmin)} <span class="text-xs text-zinc-500 dark:text-zinc-400">（主管理员）</span></td>
    <td class="px-4 py-3"><span class="yb-badge yb-badge-info">管理员</span></td>
    <td class="px-4 py-3 text-zinc-500 dark:text-zinc-400">—</td>
    <td class="px-4 py-3 text-xs text-zinc-500 dark:text-zinc-400 sticky right-0 bg-zinc-50/50 dark:bg-zinc-700/30 shadow-[-4px_0_6px_-4px_rgba(0,0,0,0.15)]">不可改</td>`;
  tbody.appendChild(builtin);
  normalFiltered.forEach(u => tbody.appendChild(userRow(u, 'usersNormal')));
  // 第三组：空用户（未提交账号）
  const vbody = $('users-vacant-tbody');
  vbody.innerHTML = '';
  $('users-vacant-empty').classList.toggle('hidden', emptyFiltered.length > 0);
  $('users-vacant-empty').textContent = emptyFiltered.length ? '' : (emptyUsers.length ? '无匹配结果' : '暂无空用户');
  $('users-vacant-count').textContent = usersVacantSearch
    ? `（${emptyFiltered.length} 人匹配 / 共 ${emptyUsers.length} 人）`
    : (emptyUsers.length ? `（${emptyUsers.length} 人）` : '');
  emptyFiltered.forEach(u => vbody.appendChild(userRow(u, 'usersVacant')));
}

// 搜索框实时过滤（输入即过滤；只过滤当前组，不影响其他组）
// 150ms 防抖：每 keystroke 全量重建 DOM 开销大，停顿后再渲染
function debounceSearch(fn) {
  clearTimeout(fn._t);
  fn._t = setTimeout(fn, 150);
}
$('pending-search').addEventListener('input', e => {
  pendingSearch = e.target.value.trim().toLowerCase();
  debounceSearch(() => renderAccounts());
});
$('active-search').addEventListener('input', e => {
  activeSearch = e.target.value.trim().toLowerCase();
  debounceSearch(() => renderAccounts());
});
$('deleted-search').addEventListener('input', e => {
  deletedSearch = e.target.value.trim().toLowerCase();
  debounceSearch(() => renderAccounts());
});
$('users-pending-search').addEventListener('input', e => {
  usersPendingSearch = e.target.value.trim().toLowerCase();
  debounceSearch(() => renderUsers(allUsers, builtinAdminName));
});
$('users-normal-search').addEventListener('input', e => {
  usersNormalSearch = e.target.value.trim().toLowerCase();
  debounceSearch(() => renderUsers(allUsers, builtinAdminName));
});
$('users-vacant-search').addEventListener('input', e => {
  usersVacantSearch = e.target.value.trim().toLowerCase();
  debounceSearch(() => renderUsers(allUsers, builtinAdminName));
});

// 用户行渲染（待审核组/正式组共用）
function userRow(u, key) {
  const tr = document.createElement('tr');
  tr.className = 'border-b border-zinc-50 dark:border-zinc-700/50 hover:bg-zinc-50 dark:hover:bg-zinc-700/50';
  const isAdmin = u.role === 'admin';
  const cb = state.batchMode ? `<td class="px-4 py-3 w-12"><input type="checkbox" class="accent-blue-500" aria-label="选择用户 ${esc(maskEmail(u.email))}" ${batchSel[key].has(u.email) ? 'checked' : ''} data-batch-key="${key}" data-batch-id="${esc(u.email)}"></td>` : '';
  const roleBadge = isAdmin
    ? '<span class="yb-badge yb-badge-info">管理员</span>'
    : '<span class="yb-badge yb-badge-neutral">普通用户</span>';
  tr.innerHTML = `
    ${cb}
    <td class="px-4 py-3 font-mono text-sm text-zinc-900 dark:text-zinc-100 whitespace-nowrap max-w-[220px] truncate" title="${esc(maskEmail(u.email))}">${esc(maskEmail(u.email))}</td>
    <td class="px-4 py-3">${roleBadge}</td>
    <td class="px-4 py-3 text-zinc-500 dark:text-zinc-400 whitespace-nowrap">${esc(u.created_at || '—')}</td>
    <td class="px-4 py-3 sticky right-0 bg-white dark:bg-zinc-800 shadow-[-4px_0_6px_-4px_rgba(0,0,0,0.15)]">
      <div class="flex items-center justify-center">
        <button aria-label="更多操作" data-email="${esc(u.email)}" data-role="${esc(u.role)}" data-key="${key}" onclick="userRowMenu(event, this)"
                class="w-9 h-9 flex items-center justify-center rounded-lg text-xl leading-none text-zinc-500 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-700 hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors duration-150">${icon('dots')}</button>
      </div>
    </td>`;
  return tr;
}

function userRowMenu(evt, btn) {
  const email = btn.dataset.email;
  const isAdmin = btn.dataset.role === 'admin';
  const key = btn.dataset.key;
  const items = [];
  // 管理员权限变更：仅主管理员 + 正式用户组可用（待审核/空用户不可设管理员）
  if (state.isMasterAdmin && key === 'usersNormal') {
    items.push({label: isAdmin ? '取消管理员' : '设为管理员', cls: isAdmin ? 'text-zinc-600 dark:text-zinc-300' : 'text-blue-600 dark:text-blue-400', fn: () => setUserRole(email, isAdmin ? 'user' : 'admin')});
  }
  items.push(
    {label: '重置密码', cls: 'text-zinc-600 dark:text-zinc-300', fn: () => resetUserPassword(email)},
    {label: '清空账号', cls: 'text-amber-600 dark:text-amber-400', fn: () => deleteUserAccounts(email)},
    {label: '删除用户', cls: 'text-red-600 dark:text-red-400', fn: () => deleteUserFull(email)},
  );
  openRowMenu(evt, items, btn);
}

async function setUserRole(email, role) {
  const action = role === 'admin' ? '设为管理员' : '取消管理员';
  // 2026-09-05：角色变更是权限面变更，接入高危门禁——须输入当前管理员密码确认
  //（批量角色变更入口已移除，本路径是唯一变更方式）
  openConfirmPasswordModal(`确定将 ${email} ${action}吗？\n请输入当前管理员密码确认。`, (cpw) => {
    api(`/api/users/${encodeURIComponent(email)}/role`, {
      method: 'POST', body: JSON.stringify({ role, confirm_password: cpw }),
    })
      .then(data => { toast(data.msg || '已更新'); loadUsers(); })
      .catch(e => toast(e.message, true));
  });
}

function resetUserPassword(email) {
  // a11y 整改：原生 prompt 弹窗 → 密码模态（遮蔽输入）；口令策略校验见 openPasswordModal set 分支
  // 管理员重置他人密码须再经 confirm 模态输入当前管理员密码二次鉴权
  openPasswordModal(`为 ${email} 设置新密码（${PW_POLICY_HINT}）`, (password) => {
    openConfirmPasswordModal(`确认重置 ${email} 的密码？\n请输入当前管理员密码确认。`, async (cpw) => {
      try {
        const data = await api(`/api/users/${encodeURIComponent(email)}/password`, {
          method: 'POST', body: JSON.stringify({ password, confirm_password: cpw }),
        });
        toast(data.msg || '密码已重置');
      } catch (e) { toast(e.message, true); }
    });
  });
}

async function deleteUserAccounts(email) {
  if (!confirm(`确定清空 ${email} 的易班账号吗？\n将解除其签到服务，用户账号保留（可重新提交）。`)) return;
  try {
    const data = await api(`/api/users/${encodeURIComponent(email)}/delete`, {
      method: 'POST', body: JSON.stringify({ mode: 'accounts_only' }),
    });
    toast(data.msg || '已清空');
    loadUsers();
    loadAccounts();
  } catch (e) { toast(e.message, true); }
}

async function deleteUserFull(email) {
  if (!confirm(`确定完全删除用户 ${email} 吗？\n将删除其账号和提交的易班账号，不可恢复！`)) return;
  // 2026-08-29 高危操作二次鉴权：须输入当前管理员密码确认
  openConfirmPasswordModal(`再次确认：完全删除 ${email}？\n请输入当前管理员密码确认。`, async (pw) => {
    try {
      const data = await api(`/api/users/${encodeURIComponent(email)}/delete`, {
        method: 'POST', body: JSON.stringify({ mode: 'full', confirm_password: pw }),
      });
      toast(data.msg || '已删除');
      loadUsers();
      loadAccounts();
    } catch (e) { toast(e.message, true); }
  });
}

// ================= 我的账号（管理员）=================
let mineAccounts = [];

async function loadMine() {
  try {
    const data = await api('/api/my-accounts');
    mineAccounts = data.accounts || [];
    renderMine();
  } catch (e) { toast(e.message, true); }
  loadMineTimePref();  // 调度 v2：管理员自选时间片（失败不阻塞）
}

