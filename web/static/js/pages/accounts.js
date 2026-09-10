// 易班自动签到 · 管理端脚本 —— 账号管理 + 批量多选（待审核 / 正常 / 待删除三组）
// 本文件是 web/static/js/app.js 的**连续区间** L246-L869，内容一字未改（仅加这 3 行头）。
// classic script、共享全局作用域：加载顺序见 templates/index.html，**顺序不可随意调整**。
// ================= 账号管理 =================
// 绑定用户：下拉列出已注册但无易班账号的用户（/api/users 过滤 account_count==0）
async function loadAvailableUsers() {
  try {
    const data = await api('/api/users');
    const available = (data.users || []).filter(u => (u.account_count || 0) === 0);
    const group = $('f-email-users');
    group.innerHTML = '';
    if (!available.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '（暂无）';
      group.appendChild(opt);
      return;
    }
    available.forEach(u => {
      const opt = document.createElement('option');
      opt.value = u.email;
      opt.textContent = `${u.email.split('@')[0]}@${u.email.split('@')[1] || ''}`;
      group.appendChild(opt);
    });
  } catch (e) { /* 静默：下拉为空也可手填 */ }
}

// 手填邮箱显示切换（初始密码随手动邮箱模式显示）
$('f-email').addEventListener('change', () => {
  const manual = $('f-email').value === '__manual__';
  $('f-email-manual').classList.toggle('hidden', !manual);
  $('f-initial-password').classList.toggle('hidden', !manual);
  $('f-initial-password-hint').classList.toggle('hidden', !manual);
});

function getEmailValue() {
  const v = $('f-email').value;
  if (v === '__manual__') return $('f-email-manual').value.trim();
  return v;
}

async function loadAccounts(_retried = false) {
  try {
    const data = await api('/api/accounts');
    state.accounts = data.accounts;
    state.states = data.states;  // /api/accounts 自带状态（键已脱敏），初始加载即填充（与轮询 refreshAccountsSilent 一致）
    state.state_msgs = data.state_msgs || {};  // 状态原因/计划时间（表格 title 展示）
    state.state_durs = data.state_durs || {};  // 单次签到耗时（P6，表格 title 展示）
    $('config-file').textContent = data.config_file;
    renderAccounts();
    fillSigninSelect();
  } catch (e) {
    // 首次加载失败（如服务重启窗口/瞬时抖动）自动重试一次，避免整页显示"待签"误导
    if (!_retried) {
      setTimeout(() => loadAccounts(true), 2000);
      return;
    }
    toast(e.message, true);
  }
}

// 各表格搜索词（内存态：10 秒轮询刷新后过滤依然生效）
let pendingSearch = '', activeSearch = '', deletedSearch = '';
let usersPendingSearch = '', usersNormalSearch = '', usersVacantSearch = '';
// 用户列表缓存（搜索事件触发时无需重新请求）
let allUsers = [];
let builtinAdminName = 'admin';

function accountMatch(a, kw) {
  if (!kw) return true;
  // phone 列表值已脱敏（138****8000）：输入完整号时同样 mask 后匹配，保证搜索可用
  return [a.name, maskPhone(a.phone), a.owner_display, a.owner].some(v => String(v || '').toLowerCase().includes(maskPhone(kw) || kw));
}

function userMatch(u, kw) {
  if (!kw) return true;
  return u.email.toLowerCase().includes(kw);
}

// ================= 批量多选（开关开启后生效） =================
const batchSel = {
  pending: new Set(), active: new Set(), deleted: new Set(),
  usersPending: new Set(), usersNormal: new Set(), usersVacant: new Set(),
};

function updateBatchBar(key) {
  const n = batchSel[key].size;
  const bar = $('batch-bar-' + key);
  if (!bar) return;
  $('batch-count-' + key).textContent = n;
}

function toggleRow(key, id, el) {
  if (el.checked) batchSel[key].add(id);
  else batchSel[key].delete(id);
  updateBatchBar(key);
}

function toggleSelectAll(key, el) {
  const sel = batchSel[key];
  sel.clear();
  if (el.checked) {
    // 仅当前组全部选中（账号按下标，用户按邮箱）
    if (key.startsWith('users')) {
      const group = key === 'usersPending'
        ? (allUsers || []).filter(u => (u.review_count || 0) > 0)
        : key === 'usersNormal'
          ? (allUsers || []).filter(u => !((u.review_count || 0) > 0) && (u.account_count || 0) > 0)
          : (allUsers || []).filter(u => !((u.review_count || 0) > 0) && (u.account_count || 0) === 0);
      group.forEach(u => sel.add(u.email));
    } else {
      const group = key === 'pending'
        ? state.accounts.filter(a => (a.status === 'pending' || a.status === 'rejected') && !a.deleted)
        : key === 'active'
          ? state.accounts.filter(a => a.status === 'active' && !a.deleted)
          : state.accounts.filter(a => a.deleted);
      group.forEach(a => sel.add(a.index));
    }
  }
  updateBatchBar(key);
  if (key.startsWith('users')) renderUsers(allUsers, builtinAdminName);
  else renderAccounts();
}

function clearBatch(key) {
  batchSel[key].clear();
  updateBatchBar(key);
  if (key.startsWith('users')) renderUsers(allUsers, builtinAdminName);
  else renderAccounts();
}

function batchAccounts(key, action) {
  const ids = [...batchSel[key]];
  if (!ids.length) { toast('请先勾选要操作的账号', true); return; }
  // 携带与 ids 对齐的 phones 供服务端防错位校验（列表漂移时整体 409 引导刷新）
  const phones = ids.map(i => (state.accounts.find(a => a.index === i) || {}).phone);
  const body = { action, ids, phones };
  if (action === 'reject') {
    const reason = prompt(`批量拒绝 ${ids.length} 个账号，请输入共同理由（用户会看到，最多 100 字）：`);
    if (reason === null) return;
    body.reason = reason.trim().slice(0, 100);
    if (!body.reason) { toast('拒绝理由不能为空', true); return; }
  }
  if (action === 'approve' && !confirm(`确定通过选中的 ${ids.length} 个账号吗？通过后将参与定时签到。`)) return;
  if (action === 'delete' && !confirm(`确定删除选中的 ${ids.length} 个账号吗？将进入待删除列表，可恢复。`)) return;
  if (action === 'purge') {
    // 彻底删除＝物理清除易班凭据，不可逆 → 与「批量删除用户」同口径走
    // 密码模态二次鉴权（后端另加高危限速与 urgent 告警）；原生长确认由模态文案承担
    openConfirmPasswordModal(
      `彻底删除选中的 ${ids.length} 个账号？\n凭据将被物理清除，不可恢复！\n请输入当前管理员密码确认。`,
      (pw) => {
        body.confirm_password = pw;
        submitBatchAccounts(key, body);
      });
    return;
  }
  // 批量手动签到：独立端点，顺序逐个执行（防风控）
  if (action === 'signin') {
    if (!confirm(`确定对选中的 ${ids.length} 个账号执行手动签到吗？将按顺序逐个执行，结果稍后出现在下方日志。`)) return;
    api('/api/signin/batch', { method: 'POST', body: JSON.stringify({ ids, phones }) })
      .then(data => { toast(data.msg || '已加入签到队列'); clearBatch(key); })
      .catch(e => toast(e.message, true));
    return;
  }
  submitBatchAccounts(key, body);
}

function submitBatchAccounts(key, body) {
  api('/api/accounts/batch', { method: 'POST', body: JSON.stringify(body) })
    .then(data => { toast(data.msg || '操作成功'); clearBatch(key); loadAccounts(); })
    .catch(e => toast(e.message, true));
}

function batchUsers(key, action) {
  const emails = [...batchSel[key]];
  if (!emails.length) { toast('请先勾选要操作的用户', true); return; }
  const body = { action, emails };
  if (action === 'reset_password') {
    // a11y 整改：原生 prompt 弹窗 → 密码模态（遮蔽输入）；校验口径见 openPasswordModal
    // 的 set 分支（补齐类别判定，与后端 _password_policy_error 一致）。
    // 重置密码 = 账号控制权转移，须再经 confirm 模态输入当前管理员密码
    openPasswordModal(`为选中的 ${emails.length} 个用户设置新密码（${PW_POLICY_HINT}）`, (pw) => {
      body.password = pw;
      openConfirmPasswordModal(`批量重置选中的 ${emails.length} 个用户密码？\n请输入当前管理员密码确认。`, (cpw) => {
        body.confirm_password = cpw;
        submitBatchUsers(key, body);
      });
    });
    return;
  }
  if (action === 'delete') {
    // 2026-08-29 高危操作二次鉴权：删除用户不可恢复，须输入当前管理员密码确认
    openConfirmPasswordModal(`批量删除选中的 ${emails.length} 个用户？\n将连同其易班账号删除，不可恢复！\n请输入当前管理员密码确认。`, (pw) => {
      body.confirm_password = pw;
      submitBatchUsers(key, body);
    });
    return;
  }
  submitBatchUsers(key, body);
}

function submitBatchUsers(key, body) {
  api('/api/users/batch', { method: 'POST', body: JSON.stringify(body) })
    .then(data => { toast(data.msg || '操作成功'); clearBatch(key); loadUsers(); })
    .catch(e => toast(e.message, true));
}

// 待删除账号表格折叠（标题栏 ▾/▸ 按钮）
function toggleDeletedCollapsed() {
  state.deletedCollapsed = !state.deletedCollapsed;
  $('accounts-deleted-table').classList.toggle('hidden', state.deletedCollapsed);
  $('deleted-toggle-btn').innerHTML = state.deletedCollapsed ? icon('chevR') : icon('chevD');
  const bar = $('batch-bar-deleted');
  if (state.deletedCollapsed) bar.classList.add('hidden');
  else if (state.batchMode) bar.classList.remove('hidden');
}

function renderAccounts() {
  // 批量开关：控制表头复选框列显示
  document.querySelectorAll('.batch-col').forEach(th => th.classList.toggle('hidden', !state.batchMode));
  document.querySelectorAll('[id^="batch-bar-"]').forEach(bar => {
    // 待删除组折叠时批量条保持隐藏（即使批量模式开启）
    const collapsed = bar.id === 'batch-bar-deleted' && state.deletedCollapsed;
    bar.classList.toggle('hidden', !state.batchMode || collapsed);
  });
  // 分组：待处理（待审核/已拒绝）置顶，正常账号一组
  // 待审核在前（新提交置顶），已拒绝沉底（同样新→旧）：accounts 表无时间戳列，
  // id 与提交先后单调一致，作时间代理。显示层排序不影响批量操作——批量提交按
  // 各账号 index + phones 对齐校验，与渲染顺序解耦。
  const pendingAccounts = state.accounts
    .filter(a => (a.status === 'pending' || a.status === 'rejected') && !a.deleted)
    .sort((a, b) => (a.status === b.status ? b.index - a.index : (a.status === 'pending' ? -1 : 1)));
  const activeAccounts = state.accounts.filter(a => a.status === 'active' && !a.deleted);
  const pendingFiltered = pendingAccounts.filter(a => accountMatch(a, pendingSearch));
  const activeFiltered = activeAccounts.filter(a => accountMatch(a, activeSearch));

  // ---- 待处理组 ----
  const pbody = $('accounts-pending-tbody');
  const pempty = $('accounts-pending-empty');
  pbody.innerHTML = '';
  pempty.classList.toggle('hidden', pendingFiltered.length > 0);
  pempty.textContent = pendingFiltered.length ? '' : (pendingAccounts.length ? '无匹配结果' : '暂无待处理账号');
  $('accounts-pending-count').textContent = pendingSearch
    ? `${pendingFiltered.length} 个匹配 / 共 ${pendingAccounts.length} 个待处理`
    : (pendingAccounts.length ? pendingAccounts.length + ' 个待处理' : '');
  pendingFiltered.forEach(a => pbody.appendChild(pendingAccountRow(a, 'pending')));

  // ---- 正常账号组 ----
  const tbody = $('accounts-tbody');
  const empty = $('accounts-empty');
  tbody.innerHTML = '';
  empty.classList.toggle('hidden', activeFiltered.length > 0);
  empty.textContent = activeFiltered.length ? '' : (activeAccounts.length ? '无匹配结果' : '暂无账号，点右上角「添加账号」配置');
  $('accounts-active-count').textContent = activeSearch
    ? `${activeFiltered.length} 个匹配 / 共 ${activeAccounts.length} 个`
    : (activeAccounts.length ? activeAccounts.length + ' 个' : '');
  activeFiltered.forEach(a => tbody.appendChild(accountRow(a, 'active')));

  // 统计卡（仅统计参与签到的正常账号；五卡：总数/成功/失败/待签/跳过）
  // 成功=success+already；待签=pending+retrying；跳过=no_task+skipped_*
  let success = 0, failed = 0, waiting = 0, skipped = 0;
  activeAccounts.forEach(a => {
    const s = state.states[a.phone] || 'pending';
    if (s === 'success' || s === 'already') success++;
    else if (s === 'failed') failed++;
    else if (s === 'no_task' || s === 'skipped_window' || s === 'skipped_norange' || s === 'paused' || s === 'user_cancelled') skipped++;
    else waiting++;
  });
  $('stat-cards').innerHTML = [
    card('生效账号', activeAccounts.length, 'text-zinc-900 dark:text-zinc-100'),
    card('今日成功', success, 'text-green-600 dark:text-green-400'),
    card('今日失败', failed, 'text-red-600 dark:text-red-400'),
    card('待签', waiting, 'text-blue-600 dark:text-blue-400'),
    card('跳过', skipped, 'text-zinc-500 dark:text-zinc-400'),
  ].join('');

  // ---- 待删除账号组（软删除，保留期内可恢复） ----
  const deletedAccounts = state.accounts.filter(a => a.deleted);
  const deletedFiltered = deletedAccounts.filter(a => accountMatch(a, deletedSearch));
  const dbody = $('accounts-deleted-tbody');
  const dempty = $('accounts-deleted-empty');
  dbody.innerHTML = '';
  dempty.classList.toggle('hidden', deletedFiltered.length > 0);
  dempty.textContent = deletedFiltered.length ? '' : (deletedAccounts.length ? '无匹配结果' : '暂无待删除账号');
  $('accounts-deleted-count').textContent = deletedSearch
    ? `${deletedFiltered.length} 个匹配 / 共 ${deletedAccounts.length} 个`
    : (deletedAccounts.length ? deletedAccounts.length + ' 个' : '');
  deletedFiltered.forEach(a => dbody.appendChild(deletedAccountRow(a, 'deleted')));

  // 顶部待处理提示
  $('pending-count').textContent = pendingAccounts.length;
  $('pending-tip').classList.toggle('hidden', pendingAccounts.length === 0);
}

function deletedAccountRow(a, key) {
  const tr = document.createElement('tr');
  tr.className = 'border-b border-zinc-50 dark:border-zinc-700/50 hover:bg-zinc-50 dark:bg-zinc-800 dark:hover:bg-zinc-700/50';
  const cb = state.batchMode ? `<td class="px-4 py-3 w-12"><input type="checkbox" class="accent-blue-500" aria-label="选择账号 ${esc(a.display_name)}" ${batchSel[key].has(a.index) ? 'checked' : ''} onchange="toggleRow('${key}', ${a.index}, this)"></td>` : '';
  tr.innerHTML = `
    ${cb}
    <td class="px-4 py-3 text-lg leading-none text-zinc-500 dark:text-zinc-400"><span class="inline-flex">${icon('trash')}</span></td>
    <td class="px-4 py-3 font-medium text-zinc-900 dark:text-zinc-100 whitespace-nowrap">${esc(a.display_name)}</td>
    <td class="px-4 py-3 font-mono text-sm text-zinc-900 dark:text-zinc-100 whitespace-nowrap">${esc(maskPhone(a.phone))}</td>
    <td class="px-4 py-3 text-zinc-500 dark:text-zinc-400 text-sm whitespace-nowrap max-w-[160px] truncate hidden md:table-cell" title="${esc(a.owner_display || (a.owner === 'admin' ? '管理员' : a.owner))}">${esc(a.owner_display || (a.owner === 'admin' ? '管理员' : a.owner))}</td>
    <td class="px-4 py-3 text-xs text-zinc-500 dark:text-zinc-400 whitespace-nowrap">${esc((a.deleted_at || '').replace('T', ' ').slice(0, 16))}</td>
    <td class="px-4 py-3 sticky right-0 bg-white dark:bg-zinc-800 shadow-[-4px_0_6px_-4px_rgba(0,0,0,0.15)]">
      <div class="flex items-center justify-center gap-1">
        <button onclick="restoreAccount(${a.index})" class="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 transition-colors duration-150">恢复</button>
        <button onclick="purgeAccount(${a.index})" class="text-xs text-red-600 dark:text-red-400 hover:text-red-700 transition-colors duration-150">彻底删除</button>
      </div>
    </td>`;
  return tr;
}

async function restoreAccount(idx) {
  const a = state.accounts[idx];
  try {
    // 携带 phone 供服务端防错位校验（列表漂移时返回 409 引导刷新）
    const data = await api(`/api/accounts/${idx}/restore`, { method: 'POST', body: JSON.stringify({ phone: a.phone }) });
    toast(data.msg || '已恢复');
    loadAccounts();
  } catch (e) { toast(e.message, true); }
}

function purgeAccount(idx) {
  const a = state.accounts[idx];
  if (!a) { toast('账号列表已变化，请刷新页面后重试', true); return; }
  // 单条物理清除此前直发请求、零鉴权零告警。现与"彻底清除已注销用户"
  // 同口径：先经密码模态取当前管理员口令，再带 confirm_password 提交
  openConfirmPasswordModal(
    `彻底删除「${a.display_name}」(${maskPhone(a.phone)})？\n凭据将被物理清除，不可恢复！\n请输入当前管理员密码确认。`,
    (pw) => submitPurgeAccount(idx, a, pw));
}

async function submitPurgeAccount(idx, a, pw) {
  try {
    // phone 供服务端防错位校验（列表漂移返回 409 引导刷新），confirm_password 为二次鉴权
    const data = await api(`/api/accounts/${idx}/purge`, {
      method: 'POST', body: JSON.stringify({ phone: a.phone, confirm_password: pw }) });
    toast(data.msg || '已彻底删除');
    loadAccounts();
  } catch (e) { toast(e.message, true); }
}

function statusBadge(a) {
  if (a.status === 'pending')
    return '<span class="yb-badge yb-badge-warning">待审核</span>';
  if (a.status === 'rejected')
    return '<span class="yb-badge yb-badge-error">已拒绝</span>';
  return '<span class="yb-badge yb-badge-success">正常</span>';
}

function pendingAccountRow(a, key) {
  const tr = document.createElement('tr');
  tr.className = 'border-b border-zinc-50 dark:border-zinc-700/50 hover:bg-zinc-50 dark:bg-zinc-800 dark:hover:bg-zinc-700/50';
  const cb = state.batchMode ? `<td class="px-4 py-3 w-12"><input type="checkbox" class="accent-blue-500" aria-label="选择账号 ${esc(a.display_name)}" ${batchSel[key].has(a.index) ? 'checked' : ''} onchange="toggleRow('${key}', ${a.index}, this)"></td>` : '';
  const reasonHtml = a.reject_reason
    ? `<div class="text-xs text-red-600 dark:text-red-400 mt-1">理由：${esc(a.reject_reason)}</div>`
    : '';
  tr.innerHTML = `
    ${cb}
    <td class="px-4 py-3 text-lg leading-none text-zinc-500 dark:text-zinc-400"><span class="inline-flex">${icon('minus')}</span></td>
    <td class="px-4 py-3 font-medium text-zinc-900 dark:text-zinc-100 whitespace-nowrap">${esc(a.display_name)}</td>
    <td class="px-4 py-3 font-mono text-sm text-zinc-900 dark:text-zinc-100 whitespace-nowrap">${esc(maskPhone(a.phone))}</td>
    <td class="px-4 py-3 text-zinc-500 dark:text-zinc-400 text-sm whitespace-nowrap max-w-[160px] truncate hidden md:table-cell" title="${esc(a.owner_display || (a.owner === 'admin' ? '管理员' : a.owner))}">${esc(a.owner_display || (a.owner === 'admin' ? '管理员' : a.owner))}</td>
    <td class="px-4 py-3">${statusBadge(a)}${reasonHtml}</td>
    <td class="px-4 py-3 sticky right-0 bg-white dark:bg-zinc-800 shadow-[-4px_0_6px_-4px_rgba(0,0,0,0.15)]">
      <div class="flex items-center justify-center">
        <button aria-label="更多操作" onclick="openRowMenu(event, [
          {label: '通过', cls: 'text-green-600 dark:text-green-400', fn: () => reviewAccount(${a.index}, 'approve')},
          {label: '拒绝', cls: 'text-red-600 dark:text-red-400', fn: () => reviewAccount(${a.index}, 'reject')},
          {label: '编辑', cls: 'text-zinc-600 dark:text-zinc-300', fn: () => openForm(${a.index})},
          {label: '删除', cls: 'text-red-600 dark:text-red-400', fn: () => deleteAccount(${a.index})},
        ])" class="w-9 h-9 flex items-center justify-center rounded-lg text-xl leading-none text-zinc-500 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-700 hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors duration-150">${icon('dots')}</button>
      </div>
    </td>`;
  return tr;
}

function accountRow(a, key) {
  const st = state.states[a.phone] || 'pending';
  const s = stateIconSvg(st);
  const stMsg = state.state_msgs ? (state.state_msgs[a.phone] || '') : '';
  const stDur = state.state_durs ? state.state_durs[a.phone] : null;
  const baseTitle = STATUS_TEXT[st] || '待签';
  // 原因/计划仅在不同于状态名时拼接（避免"签到成功 · 签到成功"式重复）；耗时存在时追加
  const stTitle = baseTitle
    + (stMsg && stMsg !== baseTitle ? ' · ' + stMsg : '')
    + (stDur ? ' · 耗时 ' + Number(stDur).toFixed(1) + 's' : '');
  const cb = state.batchMode ? `<td class="px-4 py-3 w-12"><input type="checkbox" class="accent-blue-500" aria-label="选择账号 ${esc(a.display_name)}" ${batchSel[key].has(a.index) ? 'checked' : ''} onchange="toggleRow('${key}', ${a.index}, this)"></td>` : '';
  const tr = document.createElement('tr');
  tr.className = 'border-b border-zinc-50 dark:border-zinc-700/50 hover:bg-zinc-50 dark:bg-zinc-800 dark:hover:bg-zinc-700/50';
  tr.innerHTML = `
    ${cb}
    <td class="px-4 py-3 text-lg leading-none text-zinc-500 dark:text-zinc-400"><span class="inline-flex" title="${esc(stTitle)}">${s}</span></td>
    <td class="px-4 py-3 text-zinc-500 dark:text-zinc-400">${a.index + 1}</td>
    <td class="px-4 py-3 font-medium text-zinc-900 dark:text-zinc-100 whitespace-nowrap">${esc(a.display_name)}</td>
    <td class="px-4 py-3 font-mono text-sm text-zinc-900 dark:text-zinc-100 whitespace-nowrap">${esc(maskPhone(a.phone))}</td>
    <td class="px-4 py-3 text-zinc-500 dark:text-zinc-400 whitespace-nowrap hidden lg:table-cell">${esc(a.phone_model || '—')}</td>
    <td class="px-4 py-3 text-sm whitespace-nowrap hidden xl:table-cell">${a.time_pref
      ? (a.time_pref_edge === 'first'
          ? '<span class="text-amber-600 dark:text-amber-400" title="最早时段：窗口首块，最先执行">最早 ' + esc(a.time_pref) + '</span>'
          : a.time_pref_edge === 'last'
            ? '<span class="text-amber-600 dark:text-amber-400" title="最后时段：临近截止，网络波动可能错过">最后 ' + esc(a.time_pref) + '</span>'
            : '<span class="text-zinc-600 dark:text-zinc-300">' + esc(a.time_pref) + '</span>')
      : '<span class="text-zinc-500 dark:text-zinc-400">—</span>'}</td>
    <td class="px-4 py-3 text-zinc-500 dark:text-zinc-400 text-sm whitespace-nowrap max-w-[160px] truncate hidden md:table-cell" title="${esc(a.owner_display || (a.owner === 'admin' ? '管理员' : a.owner))}">${esc(a.owner_display || (a.owner === 'admin' ? '管理员' : a.owner))}</td>
    <td class="px-4 py-3">${statusBadge(a)}</td>
    <td class="px-4 py-3 sticky right-0 bg-white dark:bg-zinc-800 shadow-[-4px_0_6px_-4px_rgba(0,0,0,0.15)]">
      <div class="flex items-center justify-center">
        <button aria-label="更多操作" onclick="accountRowMenu(event, this, ${a.index})"
                class="w-9 h-9 flex items-center justify-center rounded-lg text-xl leading-none text-zinc-500 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-700 hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors duration-150">${icon('dots')}</button>
      </div>
    </td>`;
  return tr;
}

function accountRowMenu(evt, btn, idx) {
  openRowMenu(evt, [
    {label: '上移', cls: 'text-zinc-600 dark:text-zinc-300', fn: () => moveAccount(idx, -1)},
    {label: '下移', cls: 'text-zinc-600 dark:text-zinc-300', fn: () => moveAccount(idx, 1)},
    {label: '手动签到', cls: 'text-blue-600 dark:text-blue-400', fn: () => doSigninByIndex(idx)},
    {label: '编辑', cls: 'text-zinc-600 dark:text-zinc-300', fn: () => openForm(idx)},
    {label: '删除', cls: 'text-red-600 dark:text-red-400', fn: () => deleteAccount(idx)},
  ], btn);
}

async function reviewAccount(idx, action) {
  const a = state.accounts[idx];
  let body = { action, phone: a.phone };
  if (action === 'reject') {
    const reason = prompt(`拒绝「${jsEscape(a.display_name)}」(${maskPhone(a.phone)})，请输入理由（用户会看到，最多 100 字）：`, '');
    if (reason === null) return;  // 用户取消
    body.reason = reason.trim().slice(0, 100);
    if (!body.reason) { toast('拒绝理由不能为空', true); return; }
  } else if (action === 'approve' && !confirm(`确定通过「${jsEscape(a.display_name)}」(${maskPhone(a.phone)}) 吗？通过后将参与定时签到。`)) {
    return;
  }
  try {
    const data = await api(`/api/accounts/${idx}/review`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
    toast(data.msg || (action === 'approve' ? '已通过' : '已拒绝'));
    loadAccounts();
  } catch (e) { toast(e.message, true); }
}

function card(label, value, color) {
  return `<div class="yb-card-sm">
            <div class="text-xs text-zinc-500 dark:text-zinc-400">${label}</div>
            <div class="text-xl md:text-2xl font-semibold tracking-tight ${color}">${value}</div>
          </div>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// 手机号脱敏：13912341234 → 139****1234（列表/弹窗展示，防隐私泄露）；已脱敏（含 *）或非 11 位原样返回（幂等）
function maskPhone(p) {
  p = String(p || '');
  if (p.includes('*')) return p;
  // 11 位手机号 → 138****8000；其他长度保留前 3 后 4，中间用 **** 遮盖
  return p.length >= 7 ? p.slice(0, 3) + '****' + p.slice(-4) : p;
}

// JS 模板字面量上下文转义：防用户可控数据（如账号名称）中的 `${...}`/反引号被执行（存储型 XSS）
function jsEscape(s) {
  return String(s).replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$\{/g, '\\${');
}

// 邮箱脱敏：abc123@example.com → abc***@example.com（保留域名；超短用户名至少留 1 位）
function maskEmail(e) {
  const s = String(e || '');
  const i = s.indexOf('@');
  if (i <= 0) return s;
  return s.slice(0, Math.min(3, i)) + '***' + s.slice(i);
}

async function moveAccount(idx, dir) {
  const a = state.accounts[idx];
  try {
    const data = await api(`/api/accounts/${idx}/move`, { method: 'POST', body: JSON.stringify({ dir, phone: a.phone }) });
    state.accounts = data.accounts;
    renderAccounts();
  } catch (e) { toast(e.message, true); }
}

async function deleteAccount(idx) {
  const a = state.accounts[idx];
  if (!confirm(`确定删除账号「${jsEscape(a.display_name)}」(${maskPhone(a.phone)}) 吗？`)) return;
  try {
    await api(`/api/accounts/${idx}`, { method: 'DELETE', body: JSON.stringify({ phone: a.phone }) });
    toast('已删除账号');
    loadAccounts();
  } catch (e) { toast(e.message, true); }
}

// ---- 账号表单（添加 / 编辑共用）----
// 清除已配置识别码（编辑模式）：标记后提交 __clear__（后端并行流已支持该标记清空字段）
let modalClearCodeFlag = false;
function toggleModalClearCode() {
  modalClearCodeFlag = !modalClearCodeFlag;
  const input = $('f-code');
  const btn = $('clear-code-btn');
  if (modalClearCodeFlag) {
    input.value = '';
    input.readOnly = true;
    input.placeholder = '提交后将清除已配置识别码';
    btn.textContent = '取消清除';
  } else {
    input.readOnly = false;
    input.placeholder = '64 位十六进制识别码';
    btn.textContent = '清除已配置识别码';
  }
}

async function openForm(index) {
  const trigger = document.activeElement;  // 记录触发元素：模态关闭时归还焦点（a11y）
  state.editingIndex = index;
  state.editSnapshot = null;  // 乐观锁快照在详情取到完整号后设置
  const a = index === null ? null : state.accounts[index];
  $('form-title').textContent = a ? `编辑账号 #${index + 1}` : '添加账号';
  $('f-name').value = a ? a.name : '';
  $('f-email').value = '';
  $('f-email-manual').value = '';
  $('f-email-manual').classList.add('hidden');
  $('f-initial-password').value = '';
  $('f-initial-password').classList.add('hidden');
  $('f-initial-password-hint').classList.add('hidden');
  $('f-email-wrap').classList.toggle('hidden', index !== null);  // 编辑时不显示邮箱（归属不变）
  if (index === null) loadAvailableUsers();  // 添加时加载可绑定用户
  if (a) {
    // 编辑：列表已脱敏，按需从详情接口取完整手机号（仅存内存，提交时还原）
    try {
      const d = await api(`/api/accounts/${index}/detail`);
      $('f-phone').value = maskPhone(d.account.phone);
      $('f-phone').dataset.full = d.account.phone;
      // 乐观锁快照：编辑打开时的账号指纹，提交时后端比对，防止并发编辑互相覆盖
      state.editSnapshot = JSON.stringify({
        name: d.account.name, phone: d.account.phone, phone_model: d.account.phone_model,
        status: d.account.status, deleted: d.account.deleted,
      });
    } catch (e) {
      $('f-phone').value = maskPhone(a.phone);  // 详情接口异常时退化为脱敏列表值
      $('f-phone').dataset.full = '';
      state.editSnapshot = null;  // 拿不到完整指纹 → 不携带快照（不做冲突校验）
      toast('无法获取账号完整信息', true);
    }
  } else {
    $('f-phone').value = '';
    $('f-phone').dataset.full = '';
  }
  $('f-phone-mask-tip').classList.toggle('hidden', index === null);  // 仅编辑时提示打码
  $('f-password').value = '';
  $('f-password').placeholder = a ? '留空表示不修改密码' : '易班登录密码';
  $('f-model').value = a ? a.phone_model : '';
  $('f-code').value = '';
  $('f-code').readOnly = false;
  $('f-code').placeholder = a && a.has_phone_code ? '留空表示不修改（已配置）' : '64 位十六进制识别码';
  modalClearCodeFlag = false;
  $('clear-code-btn').classList.toggle('hidden', !(a && a.has_phone_code));
  openModal($('modal-account'), trigger);  // 首个可交互元素即 f-name，打开自动聚焦
}

function closeForm() {
  closeModal($('modal-account'));
}

let accountSubmitting = false;  // 在途锁（参照 scheduleSaving 模式）：提交期间按钮禁用，防连点重复提交
$('account-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (accountSubmitting) return;
  const submitBtn = e.target.querySelector('button[type="submit"]');
  // 手机号打码显示时（未修改）用内存中的完整号码提交，避免 **** 入库
  const phoneInput = $('f-phone');
  const phoneRaw = phoneInput.value.trim();
  // 编辑态下 state.editSnapshot 为 null，说明详情接口失败——既拿不到完整
  // 手机号，也拿不到乐观锁快照。原先此路径仍允许保存（不带 _snapshot 提交），列表
  // 一旦漂移就会把改动落到另一账号上且后端无从校验；现一律禁止提交并提示刷新。
  // 两道拦截都放在启用在途锁之前：命中时按钮与 accountSubmitting 均保持原状，
  // 否则一次失败提交会把表单永久锁死（原实现的提前 return 漏了 finally）。
  if (state.editingIndex !== null && !state.editSnapshot) {
    toast('无法获取账号完整信息，请刷新页面后重试', true);
    return;
  }
  if (phoneRaw.includes('****') && !phoneInput.dataset.full) {
    toast('无法获取账号完整信息，请刷新页面后重试', true);
    return;
  }
  const btnText = submitBtn.textContent;
  accountSubmitting = true;
  submitBtn.disabled = true;
  submitBtn.textContent = '提交中…';
  const phone = phoneRaw.includes('****') && phoneInput.dataset.full ? phoneInput.dataset.full : phoneRaw;
  const body = {
    name: $('f-name').value,
    email: getEmailValue(),
    initial_password: $('f-initial-password').value,  // 手填未注册邮箱时的首登密码（替代明文临时密码）
    phone,
    password: $('f-password').value,
    phone_model: $('f-model').value.trim(),
    // 已标记清除识别码 → 提交 __clear__ 由后端清空字段；否则留空表示不修改
    phone_code: modalClearCodeFlag ? '__clear__' : $('f-code').value.trim(),
  };
  try {
    if (state.editingIndex === null) {
      const data = await api('/api/accounts', { method: 'POST', body: JSON.stringify(body) });
      toast(data.msg || '账号已添加', !data.msg);
    } else {
      // 编辑：携带乐观锁快照（打开表单时的账号指纹），后端比对不一致返回 409 阻止覆盖
      if (state.editSnapshot) body._snapshot = state.editSnapshot;
      await api(`/api/accounts/${state.editingIndex}`, { method: 'PUT', body: JSON.stringify(body) });
      toast('账号已更新');
    }
    state.editSnapshot = null;
    closeForm();
    loadAccounts();
  } catch (err) { toast(err.message, true); }
  finally {
    accountSubmitting = false;
    submitBtn.disabled = false;
    submitBtn.textContent = btnText;  // 模态已关/仍开均可安全恢复，供下次打开
  }
});

