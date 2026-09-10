// 易班自动签到 · 管理端脚本 —— 系统设置 / SMTP 发信条目编辑器 / 探针模式
// 本文件是 web/static/js/app.js 的**连续区间** L1059-L1934，内容一字未改（仅加这 3 行头）。
// classic script、共享全局作用域：加载顺序见 templates/index.html，**顺序不可随意调整**。
// ================= 系统设置 =================
// 容量统计（2026-09-08 单档口径）：签到容量 = 按有效窗口（已扣掐头去尾）与当前
// 账号间隔估算的可容纳活跃账号数；已用（或含潜在负载）超容量仅警示变色，不阻断操作
// 2026-09-10（需求3）：额外展示**名额占用**（账号 N/上限）及三分类拆解，帮助判断
// 「停签/故障账号占满名额、新账号被拒但实际负载不高」。纯展示，不参与任何配额判定。
function renderCapacity(est, cap) {
  const box = $('capacity-content');
  if (!box) return;
  if (!est) { box.innerHTML = '<div class="text-xs text-zinc-500 dark:text-zinc-400 px-1 py-2">暂无预估数据</div>'; return; }
  const cur = est.current_accounts || 0, capN = est.accounts_cap || 0, load = est.potential_load || 0;
  // cap=0（窗口退化到容纳不下一次签到）且已用>0 同样视为超限，不因 cap>0 短路漏报
  const over = cur > capN || cur + load > capN;
  const border = over ? 'border-red-300 dark:border-red-800' : 'border-zinc-200 dark:border-zinc-700';
  const text = over ? 'text-red-600 dark:text-red-400' : 'text-zinc-900 dark:text-zinc-100';
  // 名额占用行（三分类计数全部服务端下发；此处一律 Number() 归一，杜绝任何字符串
  // 被拼进 innerHTML——即便后端将来误传字符串也不会形成注入面）
  let quotaHtml = '';
  if (cap && typeof cap === 'object') {
    const used = Number(cap.accounts) || 0;
    const lim = Number(cap.accounts_max) || 0;
    const bd = cap.accounts_breakdown || {};
    const nNormal = Number(bd.normal) || 0;
    const nUserPaused = Number(bd.user_paused) || 0;
    const nCredPaused = Number(bd.cred_paused) || 0;
    const near = lim > 0 && used >= lim;
    const quotaText = lim > 0 ? `${used}/${lim}` : `${used}/不限`;
    quotaHtml = `<div class="mt-2 pt-2 border-t border-zinc-100 dark:border-zinc-700 text-xs">
      <span class="${near ? 'text-amber-600 dark:text-amber-400 font-semibold' : 'text-zinc-500 dark:text-zinc-400'}">账号容量 ${quotaText}</span>
      <span class="text-zinc-500 dark:text-zinc-400">（正常 ${nNormal} · 自暂停 ${nUserPaused} · 账密故障暂停 ${nCredPaused}）</span>
      <div class="text-zinc-500 dark:text-zinc-400">占额含自暂停与账密故障暂停账号；如需释放名额可在账号管理页清理</div>
    </div>`;
  }
  box.innerHTML = `<div class="rounded-lg border ${border} ${over ? 'bg-red-50 dark:bg-red-900/10' : ''} p-3">
    <div class="text-xs text-zinc-500 dark:text-zinc-400">签到容量（按当前账号间隔估算）</div>
    <div class="mt-1 font-semibold ${text}">已用 ${cur} <span class="text-xs font-normal text-zinc-500 dark:text-zinc-400">/ 容量 ${capN} 个</span></div>
    <div class="text-xs ${over ? 'text-red-500 dark:text-red-400' : 'text-zinc-500 dark:text-zinc-400'}">另有 ${load} 人已注册未提交（潜在负载）${over ? '；已超容量：保存更大的账号间隔将被拒绝' : ''}</div>
    ${quotaHtml}
  </div>`;
}

async function loadSettings() {
  try {
    // 预填当前公告
    const ann = await fetch(BASE + '/api/announcement').then(r => r.json()).catch(() => ({}));
    if (ann && ann.text) $('announcement-input').value = ann.text;
    await loadMailConfig();  // 邮箱通知状态（全局/个人开关）
    await loadNotifyConfig();  // 消息推送状态（Server酱/自定义 URL，v0.26.0）
    const data = await api('/api/settings');
    state.delays.gap = data.gap_max;
    renderCapacity(data.capacity_estimate, data.capacity);
    // 容量上限（2026-09-08，主管理员专属控件；现值随 capacity 区块下发）
    const capLimits = $('capacity-limits');
    if (capLimits) {
      capLimits.classList.toggle('hidden', !state.isMasterAdmin);
      state.maxUsers = data.capacity?.users_max ?? 0;
      state.maxAccounts = data.capacity?.accounts_max ?? 0;
      $('max-users-input').value = state.maxUsers;
      $('max-accounts-input').value = state.maxAccounts;
    }
    // 调度 v2：排序×分布×缓冲×自选总开关×窗口
    state.signOrder = data.sign_order || 'sequence';
    state.signDist = data.sign_dist || 'uniform';
    state.edgeFrontMin = (data.edge_front_sec ?? data.window_edge_sec ?? 60) / 60;
    state.edgeBackMin = (data.edge_back_sec ?? data.window_edge_sec ?? 60) / 60;
    state.allowTimePref = !!data.allow_time_pref;
    state.signWindow = data.sign_window || '';
    $('sign-order-select').value = state.signOrder;
    $('sign-dist-select').value = state.signDist;
    $('edge-front').value = state.edgeFrontMin;
    $('edge-back').value = state.edgeBackMin;
    // 顶部"签到窗口"静态展示与实际配置联动（2026-08-15 对抗性审查：原为写死默认值误导）
    const swEl = $('sign-window-static');
    if (swEl) swEl.textContent = state.signWindow || '--:-- ~ --:--';
    const win = state.signWindow.split('~').map(s => s.trim());
    $('window-start').value = (win[0] || '06:30').slice(0, 5);
    $('window-end').value = (win[1] || '07:50').slice(0, 5);
    renderTimePrefUI();
    updateEdgeWarn();
    renderSchedulePerm();  // 调度权限：仅主管理员可改
    state.sundaySign = !!data.sunday_sign;  // 周日签到开关（持久化 .env）
    renderSundaySignUI();
    state.saturdaySign = data.saturday_sign === 1;  // 周六签到开关（持久化 .env，默认关闭）
    renderSaturdaySignUI();
    state.globalPause = !!data.global_pause;  // 全局暂停（一键暂停签到）
    renderGlobalPauseUI();
    state.registrationPause = !!data.registration_pause;  // 暂停注册（v0.26.3）
    renderRegPauseUI();
    // 探针模式 + 注册账号验证（v0.23.x，任意管理员可改）
    $('account-verify-switch').checked = !!data.account_verify;
    $('probe-enable-switch').checked = !!data.probe_enable;
    $('probe-time').value = (data.probe_time || '20:00').slice(0, 5);
    $('probe-interval').value = data.probe_interval || '1';
    // 批量开关（会话级）不在此重置：state.batchMode 初始 false 已保证"刷新页面后关闭"；
    // 此前无条件重置导致"切 tab 再切回设置页开关失效"（2026-08-15 用户反馈）
    // 账号间隔（v0.30.0 由随机延迟卡并入调度卡）：0=关闭，直接回显现值
    $('gap-delay-secs').value = state.delays.gap;
    renderBatchModeUI();
    updateSignModeHint();
    // 重置调度脏标记：重新加载以服务器值为准（防陈旧"未保存"提示，2026-08-15 对抗性审查 F-1）
    schedDirty = false;
    $('schedule-save-btn').classList.add('hidden');
    $('sched-dirty-tip').classList.add('hidden');
    // 批量开关影响表格复选框渲染：加载完成后重绘（页面初始加载也会执行）
    renderAccounts();
    renderUsers(allUsers, builtinAdminName);
  } catch (e) { toast(e.message, true); }
}

// 调度设置：显式保存 + 确认（2026-08-15 确认）；改动只标记脏，点「保存调度设置」才写入
let schedDirty = false;
function markSchedDirty() {
  schedDirty = true;
  $('schedule-save-btn').classList.remove('hidden');
  $('sched-dirty-tip').classList.remove('hidden');
}

// 调度权限：仅主管理员可改（后端 403 兜底，前端禁用控件）；
// 账号间隔随随机延迟卡并入本卡（同为主管理员专属），沿用同一禁用清单
function renderSchedulePerm() {
  const disabled = !state.isMasterAdmin;
  ['sign-order-select', 'sign-dist-select', 'edge-front', 'edge-back', 'window-start', 'window-end',
   'gap-delay-secs', 'time-pref-toggle-btn', 'schedule-reset-btn', 'schedule-save-btn'].forEach(id => {
    const el = $(id);
    if (el) el.disabled = disabled;
  });
  document.querySelectorAll('.edge-chip').forEach(b => { b.disabled = disabled; });
  const tip = $('sched-perm-tip');
  if (tip) tip.classList.toggle('hidden', !disabled);
}

// 在途锁：防连点重复 confirm/POST（2026-08-15 对抗性审查 F-16）
let scheduleSaving = false;
async function saveScheduleSettings() {
  if (scheduleSaving) return;
  if (!confirm('保存调度设置？\n修改将在下次自动签到时生效。')) return;
  scheduleSaving = true;
  $('schedule-save-btn').disabled = true;
  try {
    const data = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({
        sign_order: state.signOrder,
        sign_dist: state.signDist,
        edge_front_sec: Math.round(state.edgeFrontMin * 60),
        edge_back_sec: Math.round(state.edgeBackMin * 60),
        allow_time_pref: state.allowTimePref ? 1 : 0,
        sign_window: state.signWindow,
      }),
    });
    schedDirty = false;
    $('schedule-save-btn').classList.add('hidden');
    $('sched-dirty-tip').classList.add('hidden');
    toast(data.msg || '调度设置已保存');
  } catch (e) { toast(e.message, true); }
  finally {
    scheduleSaving = false;
    // 仅主管理员才恢复可点（renderSchedulePerm 权限语义）
    if (state.isMasterAdmin) $('schedule-save-btn').disabled = false;
  }
}

// 排序/分布/缓冲/窗口：改动只标记（显式保存）
$('sign-order-select').addEventListener('change', () => {
  state.signOrder = $('sign-order-select').value;
  markSchedDirty();
});
$('sign-dist-select').addEventListener('change', () => {
  state.signDist = $('sign-dist-select').value;
  markSchedDirty();
});
// 掐头/去尾：改动只标记（显式保存），输入自动钳制 0-5 并对齐 0.5 步进
['edge-front', 'edge-back'].forEach(id => {
  $(id).addEventListener('change', () => {
    const v = parseFloat($(id).value);
    if (isNaN(v)) return;
    const clamped = Math.min(5, Math.max(0, v));
    const snapped = Math.round(clamped * 2) / 2;  // 0.5 分钟粒度
    $(id).value = snapped;
    if (id === 'edge-front') state.edgeFrontMin = snapped; else state.edgeBackMin = snapped;
    updateEdgeWarn();
    markSchedDirty();
  });
});
// 快捷档位（一键设置前后相同值）
document.querySelectorAll('.edge-chip').forEach(btn => {
  btn.addEventListener('click', () => {
    const m = parseFloat(btn.dataset.edgeMin);
    state.edgeFrontMin = state.edgeBackMin = m;
    $('edge-front').value = m;
    $('edge-back').value = m;
    updateEdgeWarn();
    markSchedDirty();
  });
});
// 窗口起止（原生 time 选择器保证格式；改动只标记，保存走「保存调度设置」）
['window-start', 'window-end'].forEach(id => {
  $(id).addEventListener('change', () => {
    const s = $('window-start').value || '06:30';
    const e = $('window-end').value || '07:50';
    state.signWindow = `${s} ~ ${e}`;
    markSchedDirty();
  });
});

// 图形开关渲染（2026-08-15 用户反馈：文字按钮→直观图形开关；aria-checked 驱动形态）
function renderToggle(id, on) {
  const btn = $(id);
  if (!btn) return;
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
}

// 用户自选时间片总开关（显式保存；关闭时用户可预配置但不激活）
function renderTimePrefUI() {
  renderToggle('time-pref-toggle-btn', state.allowTimePref);
}
function toggleTimePref() {
  state.allowTimePref = !state.allowTimePref;
  renderTimePrefUI();
  markSchedDirty();  // 显式保存：不直接写入
}
function updateEdgeWarn() {
  const warn = $('edge-warn');
  if (warn) warn.classList.toggle('hidden', state.edgeFrontMin !== 0 && state.edgeBackMin !== 0);
}

// 恢复默认调度设置（窗口/掐头去尾/排序/分布；自选开关不动——那是功能开关）；标记脏，随保存按钮提交
function resetScheduleSettings() {
  if (!confirm('恢复默认调度设置？\n窗口 06:30 ~ 07:50 · 掐头去尾各 1 分钟 · 排序：顺序 · 分布：均匀')) return;
  state.signOrder = 'sequence';
  state.signDist = 'uniform';
  state.edgeFrontMin = 1;
  state.edgeBackMin = 1;
  state.signWindow = '06:30 ~ 07:50';
  $('sign-order-select').value = state.signOrder;
  $('sign-dist-select').value = state.signDist;
  $('edge-front').value = state.edgeFrontMin;
  $('edge-back').value = state.edgeBackMin;
  $('window-start').value = '06:30';
  $('window-end').value = '07:50';
  updateEdgeWarn();
  markSchedDirty();
  toast('已恢复默认值，点击「保存调度设置」生效');
}

// 随机模式下账号管理页提示：顺序不影响执行（调度 v2 判定来源为 sign_order，2026-08-15 对抗性审查 F-9）
function updateSignModeHint() {
  const hint = $('sign-mode-hint');
  if (hint) hint.classList.toggle('hidden', state.signOrder !== 'random');
}

function renderSundaySignUI() {
  renderToggle('sunday-sign-btn', state.sundaySign);
}

function toggleSundaySign() {
  state.sundaySign = !state.sundaySign;
  renderSundaySignUI();
  saveSettings(true);  // 自动保存（写入 .env，cron 下次触发生效）
}

function renderSaturdaySignUI() {
  renderToggle('saturday-sign-btn', state.saturdaySign);
}

function toggleSaturdaySign() {
  state.saturdaySign = !state.saturdaySign;
  renderSaturdaySignUI();
  saveSettings(true);  // 自动保存（写入 .env，cron 下次触发生效）
}

// 全局暂停（一键暂停签到）：仅主管理员；双重确认；下一轮 cron 生效
function renderGlobalPauseUI() {
  // 普通管理员：危险区整卡隐藏（仅主管理员可一键暂停，v0.26.0）
  const card = $('danger-zone-card');
  if (card) card.classList.toggle('hidden', !state.isMasterAdmin);
  const btn = $('global-pause-btn');
  const hint = $('global-pause-hint');
  if (!btn) return;
  // 仅主管理员可操作（后端 403 兜底）
  btn.disabled = !state.isMasterAdmin;
  if (state.globalPause) {
    $('global-pause-btn-icon').innerHTML = icon('play');
    $('global-pause-btn-text').textContent = '恢复自动签到';
    btn.classList.remove('bg-red-600', 'hover:bg-red-700');
    btn.classList.add('bg-green-600', 'hover:bg-green-700');
  } else {
    $('global-pause-btn-icon').innerHTML = icon('pause');
    $('global-pause-btn-text').textContent = '暂停自动签到';
    btn.classList.remove('bg-green-600', 'hover:bg-green-700');
    btn.classList.add('bg-red-600', 'hover:bg-red-700');
  }
  if (hint) hint.classList.toggle('hidden', !state.globalPause);
}

async function toggleGlobalPause() {
  if (!state.isMasterAdmin) return;
  const next = !state.globalPause;
  // 第一次确认：说明影响
  const msg1 = next
    ? '【暂停签到】\n\n所有账号将停止自动签到。\n· 当前正在运行的进程会跑完\n· 手动签到不受影响\n· 可随时恢复\n\n确认继续？'
    : '【恢复签到】\n\n下一轮自动签到将恢复执行。\n\n确认继续？';
  if (!confirm(msg1)) return;
  // 第二次确认：明确选择
  const msg2 = next
    ? '注意：再次确认，确定要【暂停】自动签到吗？\n\n此操作将立即保存，下一次自动签到起生效。'
    : '注意：再次确认，确定要【恢复】自动签到吗？';
  if (!confirm(msg2)) return;
  try {
    const data = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ global_pause: next ? 1 : 0 }),
    });
    state.globalPause = next;
    renderGlobalPauseUI();
    toast(data.msg || (next ? '签到已暂停' : '签到已恢复'));
  } catch (e) { toast(e.message, true); }
}

// 暂停注册（v0.26.3）：仅主管理员；双重确认；立即生效（注册 API 即时拦截）
function renderRegPauseUI() {
  const btn = $('reg-pause-btn');
  const hint = $('reg-pause-hint');
  if (!btn) return;
  btn.disabled = !state.isMasterAdmin;
  if (state.registrationPause) {
    $('reg-pause-btn-icon').innerHTML = icon('play');
    $('reg-pause-btn-text').textContent = '开放注册';
    btn.classList.remove('bg-red-600', 'hover:bg-red-700');
    btn.classList.add('bg-green-600', 'hover:bg-green-700');
  } else {
    $('reg-pause-btn-icon').innerHTML = icon('pause');
    $('reg-pause-btn-text').textContent = '暂停注册';
    btn.classList.remove('bg-green-600', 'hover:bg-green-700');
    btn.classList.add('bg-red-600', 'hover:bg-red-700');
  }
  if (hint) hint.classList.toggle('hidden', !state.registrationPause);
}

async function toggleRegPause() {
  if (!state.isMasterAdmin) return;
  const next = !state.registrationPause;
  const msg1 = next
    ? '【暂停注册】\n\n登录页将关闭注册入口，新用户无法自助注册。\n· 已注册用户登录不受影响\n· 你仍可在「账号管理」为用户手动添加账号\n\n确认继续？'
    : '【开放注册】\n\n登录页将恢复注册入口，任何人可按现有规则注册。\n\n确认继续？';
  if (!confirm(msg1)) return;
  const msg2 = next
    ? '注意：再次确认，确定要【暂停】注册吗？\n\n此操作立即生效。'
    : '注意：再次确认，确定要【开放】注册吗？';
  if (!confirm(msg2)) return;
  try {
    const data = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ registration_pause: next ? 1 : 0 }),
    });
    state.registrationPause = next;
    renderRegPauseUI();
    toast(data.msg || (next ? '注册已暂停' : '注册已开放'));
  } catch (e) { toast(e.message, true); }
}

function renderBatchModeUI() {
  renderToggle('batch-mode-btn', state.batchMode);
}

function toggleBatchMode() {
  state.batchMode = !state.batchMode;
  renderBatchModeUI();
  renderAccounts();
  renderUsers(allUsers, builtinAdminName);
  // 会话级开关：不写入配置，刷新/重进页面自动恢复关闭
}

// 移动端完整表格开关（v0.30.0）：body.full-tables 类驱动 CSS 强制显示 md:table-cell 列，
// localStorage（yiban-full-tables）记忆；受限环境降级为不记忆、仅本次会话生效
function toggleFullTables() {
  const btn = $('full-tables-btn');
  const on = btn.getAttribute('aria-checked') !== 'true';
  try {
    if (on) localStorage.setItem('yiban-full-tables', '1');
    else localStorage.removeItem('yiban-full-tables');
  } catch (e) {}
  document.body.classList.toggle('full-tables', on);
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
}

function initFullTables() {
  let on = false;
  try { on = localStorage.getItem('yiban-full-tables') === '1'; } catch (e) {}
  document.body.classList.toggle('full-tables', on);
  const btn = $('full-tables-btn');
  if (btn) btn.setAttribute('aria-checked', on ? 'true' : 'false');
}

async function saveSettings(silent) {
  // 周末开关所有管理员可改；账号间隔与容量上限属主管理员专属参数，仅主管理员
  // 在变更时携带对应键——后端「见键即要求主管理员密码确认」（容量上限还并入
  // 403 字段清单），不带键则不修改
  const payload = {
    // 注意：不发送 sign_mode——遗留字段（已无控件），发送默认值 'sequence' 会
    // 静默覆盖 .env 中既有的 YIBAN_SIGN_MODE，且权限上仅主管理员可写
    sunday_sign: state.sundaySign ? 1 : 0,
    saturday_sign: state.saturdaySign ? 1 : 0,
  };
  const gapRaw = parseInt($('gap-delay-secs').value, 10);
  const gapV = isNaN(gapRaw) || gapRaw < 0 ? state.delays.gap : gapRaw;  // 空/非法 = 沿用现值；0=关闭
  const gapChanged = state.isMasterAdmin && gapV !== state.delays.gap;
  // 容量上限（2026-09-08）：仅主管理员可见可改；非法/空输入沿用现值
  const capVal = (id, cur) => {
    const v = parseInt($(id)?.value, 10);
    return isNaN(v) || v < 0 ? cur : v;
  };
  const maxUserV = capVal('max-users-input', state.maxUsers);
  const maxAccV = capVal('max-accounts-input', state.maxAccounts);
  const maxUserChanged = state.isMasterAdmin && maxUserV !== state.maxUsers;
  const maxAccChanged = state.isMasterAdmin && maxAccV !== state.maxAccounts;
  try {
    let data;
    if (gapChanged || maxUserChanged || maxAccChanged) {
      data = await new Promise((resolve, reject) => {
        openConfirmPasswordModal('调整签到节奏/容量上限：不合适的设置可能影响签到成功率，是否继续？\n请输入当前主管理员密码确认。', async (cpw) => {
          try {
            if (gapChanged) payload.gap_max = gapV;
            if (maxUserChanged) payload.max_users = maxUserV;
            if (maxAccChanged) payload.max_accounts = maxAccV;
            payload.confirm_password = cpw;
            resolve(await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) }));
          } catch (err) { reject(err); }
        });
      });
    } else {
      // 未变更不带 gap/max_* 键（避免无谓触发后端口令/容量校验路径）
      data = await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) });
    }
    state.delays.gap = gapV;
    $('gap-delay-secs').value = gapV;
    if (maxUserChanged || maxAccChanged) {
      state.maxUsers = maxUserV;
      state.maxAccounts = maxAccV;
      $('max-users-input').value = maxUserV;
      $('max-accounts-input').value = maxAccV;
    }
    if (gapChanged || maxUserChanged || maxAccChanged) {
      // POST 返回体不含容量预估：间隔/上限变化后补拉一次，概览卡容量统计保持新鲜
      const fresh = await api('/api/settings');
      renderCapacity(fresh.capacity_estimate, fresh.capacity);
    }
    if (!silent) {
      toast(data.msg || '设置已保存');
    } else {
      const tip = $('settings-saved-tip');
      tip.classList.remove('hidden');
      clearTimeout(tip._t);
      tip._t = setTimeout(() => tip.classList.add('hidden'), 2000);
    }
  } catch (e) { if (!silent) toast(e.message, true); }
}

// 全局公告编辑（管理员）：保存到 .env；loadSettings 时预填当前公告
async function saveAnnouncement() {
  const text = $('announcement-input').value.trim();
  try {
    const data = await api('/api/announcement', {
      method: 'PUT', body: JSON.stringify({ text }),
    });
    $('announcement-tip').textContent = data.msg || '已更新';
    setTimeout(() => { $('announcement-tip').textContent = ''; }, 3000);
  } catch (e) { toast(e.message, true); }
}

// ===== 邮箱通知（v0.23.0）：全局开关（主管理员）+ 个人开关（普通管理员）=====
let mailEnabled = false;   // 全局（YIBAN_MAIL_ENABLE）
let mailSelfOn = true;     // 个人（users.mail_notify，普通管理员）

async function loadMailConfig() {
  try {
    const data = await api('/api/mail-config');
    mailEnabled = !!data.enabled;
    $('mail-config-status').textContent = data.enabled
      ? `已开启 · 发件 ${data.user} · 告警收件 ${data.admin_to}`
      : '未开启（需先在下方配置发件 SMTP，再由主管理员开启）';
    $('mail-global-wrap').classList.toggle('hidden', !state.isMasterAdmin);
    if (state.isMasterAdmin) $('mail-global-switch').checked = mailEnabled;
    // 个人开关：主管理员 = YIBAN_MAIL_ADMIN_NOTIFY；普通管理员 = users.mail_notify
    $('mail-self-wrap').classList.remove('hidden');
    if (state.isMasterAdmin) mailSelfOn = !!data.admin_notify;
    $('mail-self-switch').checked = mailSelfOn;
    // SMTP 发信条目列表（v0.30.0）：仅主管理员展示编辑器（user 已打码，pass 不回显）
    state.mailSmtps = data.smtps || [];
    $('smtp-editor-wrap').classList.toggle('hidden', !state.isMasterAdmin);
    if (state.isMasterAdmin) renderSmtps();
    // 告警收件人（v0.30.0 修复）：仅主管理员；打码值只作 placeholder，输入框恒为空
    $('mail-admin-to-wrap').classList.toggle('hidden', !state.isMasterAdmin);
    if (state.isMasterAdmin) {
      const toEl = $('mail-admin-to');
      toEl.value = '';
      toEl.placeholder = data.admin_to || 'admin@example.com';
    }
  } catch (e) { /* 配置读取失败不阻塞设置页 */ }
}

// 保存告警收件人（v0.30.0 修复）：改收件人 = 改告警送达路径，须主管理员口令确认。
// 留空不提交（避免误清空已配置地址），清空走 clearAdminTo。
function saveAdminTo() {
  const el = $('mail-admin-to');
  const val = (el.value || '').trim();
  if (!val) { toast('请填写收件人邮箱；如需清空请点「清空」', true); return; }
  openConfirmPasswordModal(
    '修改告警收件人？\n告警邮件将改发到新地址，原收件人会被通知。\n请输入当前主管理员密码确认。',
    async (pw) => {
      const tip = $('mail-config-tip');
      tip.textContent = '保存中…';
      try {
        await api('/api/mail-config', { method: 'PUT', body: JSON.stringify({ admin_to: val, confirm_password: pw }) });
        tip.textContent = '已保存告警收件人';
        setTimeout(() => { tip.textContent = ''; }, 3000);
        await loadMailConfig();
      } catch (e) {
        tip.textContent = '';
        toast(e.message, true);
      }
    }
  );
}

// 清空告警收件人：显式提交空串（后端键存在即按提交值落盘）
function clearAdminTo() {
  if (!confirm('清空告警收件人？\n清空后将不再发送管理员告警邮件（除非另有开启接收的管理员）。')) return;
  openConfirmPasswordModal(
    '清空告警收件人？\n清空后管理员告警邮件将无人接收！\n请输入当前主管理员密码确认。',
    async (pw) => {
      const tip = $('mail-config-tip');
      tip.textContent = '保存中…';
      try {
        await api('/api/mail-config', { method: 'PUT', body: JSON.stringify({ admin_to: '', confirm_password: pw }) });
        tip.textContent = '已清空告警收件人';
        setTimeout(() => { tip.textContent = ''; }, 3000);
        await loadMailConfig();
      } catch (e) {
        tip.textContent = '';
        toast(e.message, true);
      }
    }
  );
}

async function toggleMailGlobal() {
  const el = $('mail-global-switch');
  const enabled = el.checked;
  // 关闭全局邮件通道 = 给全部安全告警拔线（后端已要求二次口令）。
  // 先把开关拨回原状，口令确认成功后再落——用户取消模态时界面不会出现"看着已关"的假象
  if (!enabled) {
    el.checked = mailEnabled;
    openConfirmPasswordModal(
      '关闭全局邮件通知？\n关闭后所有安全告警都不再发邮件，且"被关闭"这件事本身也可能没人知道！\n请输入当前主管理员密码确认。',
      (pw) => { el.checked = false; submitMailGlobal(false, pw); }
    );
    return;
  }
  await submitMailGlobal(enabled, null);
}

async function submitMailGlobal(enabled, pw) {
  const tip = $('mail-config-tip');
  tip.textContent = '保存中…';
  const body = { enabled };
  if (pw) body.confirm_password = pw;
  try {
    await api('/api/mail-config', { method: 'PUT', body: JSON.stringify(body) });
    mailEnabled = enabled;
    tip.textContent = enabled ? '已开启全局邮件通知' : '已关闭全局邮件通知';
    setTimeout(() => { tip.textContent = ''; }, 3000);
  } catch (e) {
    $('mail-global-switch').checked = mailEnabled;  // 回滚
    tip.textContent = '保存失败：' + e.message;
  }
}

async function toggleMailSelf() {
  const el = $('mail-self-switch');
  const enabled = el.checked;
  // 主管理员关闭个人接收（admin_notify=false）后 ADMIN_TO 告警邮件全部
  // 停发，同样属"拔线"动作 → 二次口令；普通管理员的 my-mail-notify 只影响其本人，不变
  if (state.isMasterAdmin && !enabled) {
    el.checked = mailSelfOn;
    openConfirmPasswordModal(
      '关闭主管理员告警邮件接收？\n关闭后 ADMIN_TO 不再收到任何告警邮件（其他管理员收件不受影响）！\n请输入当前主管理员密码确认。',
      (pw) => { el.checked = false; submitMailSelf(false, pw); }
    );
    return;
  }
  await submitMailSelf(enabled, null);
}

async function submitMailSelf(enabled, pw) {
  const tip = $('mail-config-tip');
  tip.textContent = '保存中…';
  try {
    if (state.isMasterAdmin) {
      const body = { admin_notify: enabled };
      if (pw) body.confirm_password = pw;
      await api('/api/mail-config', { method: 'PUT', body: JSON.stringify(body) });
    } else {
      await api('/api/my-mail-notify', { method: 'PUT', body: JSON.stringify({ enabled }) });
    }
    mailSelfOn = enabled;
    tip.textContent = enabled ? '已开启接收邮件提醒' : '已关闭接收邮件提醒';
    setTimeout(() => { tip.textContent = ''; }, 3000);
  } catch (e) {
    $('mail-self-switch').checked = mailSelfOn;  // 回滚
    tip.textContent = '保存失败：' + e.message;
  }
}

// ===== SMTP 发信条目编辑器（v0.29.0：主备 failover 列表，仅主管理员） =====
// GET 返回的 user 已打码（含 *）、未配置为「<未配置>」占位——这两种形态的串
// 不允许作为真值提交（否则按字面落盘损坏配置），收集时一律归一为空串 = 后端沿用旧值
function _smtpClean(v) {
  const s = String(v || '').trim();
  return !s || s.includes('*') || s.startsWith('<') ? '' : s;
}

function renderSmtps() {
  const box = $('smtps-list');
  box.innerHTML = '';
  state.mailSmtps.forEach((e, i) => {
    const row = document.createElement('div');
    row.className = 'rounded-lg border border-zinc-200 dark:border-zinc-700 p-3 space-y-2';
    row.innerHTML = `
      <div class="flex items-center justify-between">
        <span class="text-xs text-zinc-500 dark:text-zinc-400">SMTP ${i + 1}${i === 0 ? '（主）' : '（备用）'}</span>
        <button type="button" onclick="removeSmtp(${i})" class="text-xs text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 transition-colors duration-150">删除</button>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-6 gap-2">
        <div class="sm:col-span-3">
          <label class="block text-[11px] text-zinc-500 dark:text-zinc-400 mb-0.5">服务器 host</label>
          <input data-f="host" value="${esc(e.host)}" placeholder="smtp.example.com" autocomplete="off" class="yb-input yb-input-sm">
        </div>
        <div>
          <label class="block text-[11px] text-zinc-500 dark:text-zinc-400 mb-0.5">端口</label>
          <input data-f="port" type="number" min="1" max="65535" value="${esc(e.port)}" placeholder="465" class="yb-input yb-input-sm">
        </div>
        <div class="sm:col-span-2">
          <label class="block text-[11px] text-zinc-500 dark:text-zinc-400 mb-0.5">发件账号（留空沿用）</label>
          <input data-f="user" value="" placeholder="${esc(e.user)}" autocomplete="off" class="yb-input yb-input-sm">
        </div>
        <div class="sm:col-span-3">
          <label class="block text-[11px] text-zinc-500 dark:text-zinc-400 mb-0.5">授权码（永不回显）</label>
          <input data-f="pass" type="password" value="" placeholder="${e.has_pass ? '已配置，留空沿用' : '未配置'}" autocomplete="new-password" class="yb-input yb-input-sm">
        </div>
      </div>`;
    box.appendChild(row);
  });
}

function addSmtp() {
  state.mailSmtps.push({ host: '', port: 465, user: '', has_pass: false });
  renderSmtps();
}

function removeSmtp(i) {
  state.mailSmtps.splice(i, 1);
  renderSmtps();
}

async function saveSmtps() {
  const entries = [...document.querySelectorAll('#smtps-list > div')].map(row => ({
    host: _smtpClean(row.querySelector('[data-f="host"]').value),
    port: parseInt(row.querySelector('[data-f="port"]').value, 10) || 465,
    user: _smtpClean(row.querySelector('[data-f="user"]').value),
    pass: row.querySelector('[data-f="pass"]').value.trim(),  // 授权码仅过滤空白；是否沿用由后端按索引决定
  }));
  openConfirmPasswordModal('保存 SMTP 配置：更换/清空邮件通道属敏感操作。\n请输入当前主管理员密码确认。', async (pw) => {
    const tip = $('mail-config-tip');
    tip.textContent = '保存中…';
    try {
      await api('/api/mail-config', { method: 'PUT', body: JSON.stringify({ smtps: entries, confirm_password: pw }) });
      tip.textContent = '已保存 SMTP 配置';
      setTimeout(() => { tip.textContent = ''; }, 3000);
      await loadMailConfig();  // 重拉打码值与全局状态（重渲染主备列表）
    } catch (e) {
      tip.textContent = '';
      toast(e.message, true);
    }
  });
}

// ===== 消息推送（Webhook：Server酱/自定义 URL，v0.26.0）=====
async function loadNotifyConfig() {
  // v0.30.0：卡对所有管理员可见（同卡「邮件通知」区的个人开关普通管理员要用）；
  // 仅主管理员专属的 Webhook 配置块隐藏。普通管理员无需拉取主管理员专属配置。
  const card = $('notify-card');
  if (card) card.classList.remove('hidden');
  $('notify-master-wrap').classList.toggle('hidden', !state.isMasterAdmin);
  if (!state.isMasterAdmin) return;
  try {
    const data = await api('/api/notify-config');
    $('notify-type').value = data.type || '';
    $('notify-secret').value = '';
    $('notify-urgent-switch').checked = !!data.urgent_only;
    // 额度已分成"非紧急 / 紧急"两本账，只报一个数字会被运维当成总额度
    const gLeft = data.daily_remaining == null ? '不限' : `${data.daily_remaining} 条`;
    const uLeft = data.urgent_daily_remaining == null ? '不限' : `${data.urgent_daily_remaining} 条`;
    if (data.daily_max != null) $('notify-daily-max').value = data.daily_max;
    if (data.urgent_daily_max != null) $('notify-urgent-daily-max').value = data.urgent_daily_max;
    const parts = [];
    if (data.enabled) {
      parts.push(`已开启（${data.type === 'serverchan' ? 'Server酱' : '自定义地址'}，密钥 ${data.secret_masked}）`);
      if (data.urgent_only) parts.push('仅推送重要告警');
    } else {
      parts.push(data.configured ? '已配置但不可用（密钥缺失或解密失败，请重新填写密钥）' : '未配置');
    }
    // 两本账剩余无论通道开关都照报：额度耗尽本身就是要让运维看见的事实
    parts.push(`今日推送额度：非紧急剩余 ${gLeft} / 紧急剩余 ${uLeft}`);
    $('notify-status').textContent = parts.join('；');
  } catch (e) { /* 读取失败不阻塞设置页 */ }
}

async function saveNotifyConfig() {
  const body = { type: $('notify-type').value };
  const secret = $('notify-secret').value.trim();
  if (secret) body.secret = secret;
  // 关闭推送：无需密钥，直接保存（后端清除类型与密钥，v0.26.0 修复原"关不掉"bug）
  // 开启推送：密钥不回显，须重新输入；同时防误点保存把已配置密钥清空
  if (body.type && !secret) { toast('开启推送请填写密钥', true); return; }
  // 关闭通道与换密钥等同"给报警器拔线"，后端要求二次口令后才落盘；
  // 只改节流/额度/仅重要告警开关不走这里（见 saveNotifyBudget / saveNotifyUrgent）
  openConfirmPasswordModal(
    body.type
      ? `保存消息推送配置（${body.type === 'serverchan' ? 'Server酱' : '自定义地址'} + 新密钥）？\n新密钥加密落盘，旧密钥立即失效。\n请输入当前主管理员密码确认。`
      : '关闭消息推送？\n关闭后所有告警不再推送到手机（邮件通知不受影响）！\n请输入当前主管理员密码确认。',
    (pw) => { body.confirm_password = pw; submitNotifyConfig(body); }
  );
}

async function submitNotifyConfig(body) {
  const tip = $('notify-tip');
  tip.textContent = '保存中…';
  try {
    const data = await api('/api/notify-config', { method: 'PUT', body: JSON.stringify(body) });
    $('notify-secret').value = '';
    tip.textContent = data.enabled ? '已保存并开启' : '已关闭';
    await loadNotifyConfig();
  } catch (e) {
    tip.textContent = '';
    toast(e.message, true);
  }
}

// 两本账每日上限（分账后紧急账也能在页面上配）：纯数值改动，无需二次口令
async function saveNotifyBudget() {
  const tip = $('notify-tip');
  const gv = String($('notify-daily-max').value).trim();
  const uv = String($('notify-urgent-daily-max').value).trim();
  if (!/^\d+$/.test(gv) || !/^\d+$/.test(uv)) { toast('每日上限须为 0 或正整数（0=不限）', true); return; }
  tip.textContent = '保存中…';
  try {
    await api('/api/notify-config', {
      method: 'PUT',
      body: JSON.stringify({ daily_max: Number(gv), urgent_daily_max: Number(uv) }),
    });
    tip.textContent = '已保存每日额度';
    setTimeout(() => { tip.textContent = ''; }, 3000);
    await loadNotifyConfig();
  } catch (e) {
    tip.textContent = '';
    toast(e.message, true);
  }
}

async function testNotify() {
  const tip = $('notify-tip');
  tip.textContent = '发送中…';
  try {
    const data = await api('/api/notify-test', { method: 'POST' });
    tip.textContent = data.msg || '已发送';
  } catch (e) {
    tip.textContent = '';
    toast(e.message, true);
  }
}

async function saveNotifyUrgent() {
  const tip = $('notify-tip');
  tip.textContent = '保存中…';
  try {
    const data = await api('/api/notify-config', {
      method: 'PUT',
      body: JSON.stringify({ urgent_only: $('notify-urgent-switch').checked }),
    });
    tip.textContent = data.urgent_only ? '已开启仅重要告警' : '已关闭（全部推送）';
    await loadNotifyConfig();
  } catch (e) {
    $('notify-urgent-switch').checked = !$('notify-urgent-switch').checked;  // 回滚
    tip.textContent = '';
    toast(e.message, true);
  }
}

// ===== 探针模式 + 注册账号验证（v0.23.x）：任意管理员可改，改动即保存 =====
let probeSaving = false;
async function saveProbeSettings() {
  if (probeSaving) return;
  const timeVal = $('probe-time').value;
  if (!/^\d{2}:\d{2}$/.test(timeVal || '')) { toast('请选择触发时间', true); return; }
  probeSaving = true;
  try {
    await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({
        account_verify: $('account-verify-switch').checked ? 1 : 0,
        probe_enable: $('probe-enable-switch').checked ? 1 : 0,
        probe_time: timeVal,
        probe_interval: $('probe-interval').value,
      }),
    });
    const tip = $('probe-config-tip');
    tip.textContent = '已保存（将在设定时间后的调度周期自动执行健康检查）';
    setTimeout(() => { tip.textContent = ''; }, 3000);
  } catch (e) { toast(e.message, true); }
  finally { probeSaving = false; }
}

async function doPing() {
  const el = $('ping-result');
  el.textContent = '检测中…';
  try {
    const data = await api('/api/ping', { method: 'POST' });
    el.innerHTML = data.reachable
      ? `<span class="text-green-600 dark:text-green-400 inline-flex items-center gap-1">${icon('check')} 易班 API 可达（${esc(data.detail)}）</span>`
      : `<span class="text-red-600 dark:text-red-400 inline-flex items-center gap-1">${icon('close')} 不可达（${esc(data.detail)}）</span>`;
  } catch (e) {
    el.innerHTML = `<span class="text-red-600 dark:text-red-400 inline-flex items-center gap-1">${icon('close')} 检测失败（${esc(e.message)}）</span>`;
  }
}

// 修改管理员账号（验证当前密码，写入 .env）
async function saveMyPassword() {
  const oldPassword = $('my-old-password').value;
  const password = $('my-new-password').value;
  // 主管理员口令单独提档（12 位三类），与后端 _admin_password_policy_error 同口径
  const adminPolicy = !!state.isMasterAdmin;
  const hint = adminPolicy ? PW_ADMIN_HINT : PW_POLICY_HINT;
  if (adminPolicy ? !passwordPolicyOkAdmin(password) : !passwordPolicyOk(password)) { toast(`新密码${hint}`, true); return; }
  if (!oldPassword) { toast('请输入当前密码验证', true); return; }
  if (password !== $('my-confirm-password').value) { toast('两次输入的新密码不一致', true); return; }
  try {
    const data = await api('/api/me/password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: password, confirm_password: $('my-confirm-password').value }),
    });
    toast(data.msg || '密码已更新');
    $('my-old-password').value = '';
    $('my-new-password').value = '';
    $('my-confirm-password').value = '';
  } catch (e) { toast(e.message, true); }
}

// 普通管理员改密（「我的账号」tab，v0.30.0）：逻辑同 saveMyPassword、仅读 mine-* 前缀；
// 入口仅非主管理员可见（主管理员走设置页危险区），故固定用普通口令策略（PW_POLICY_HINT）
async function saveMinePassword() {
  const oldPassword = $('mine-password-now').value;
  const password = $('mine-new-password').value;
  if (!passwordPolicyOk(password)) { toast(`新密码${PW_POLICY_HINT}`, true); return; }
  if (!oldPassword) { toast('请输入当前密码验证', true); return; }
  if (password !== $('mine-new-password2').value) { toast('两次输入的新密码不一致', true); return; }
  try {
    const data = await api('/api/me/password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: password, confirm_password: $('mine-new-password2').value }),
    });
    toast(data.msg || '密码已更新');
    $('mine-password-now').value = '';
    $('mine-new-password').value = '';
    $('mine-new-password2').value = '';
  } catch (e) { toast(e.message, true); }
}
