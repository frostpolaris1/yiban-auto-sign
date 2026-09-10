// 易班自动签到 · 管理端脚本 —— 签到日志与状态
// 本文件是 web/static/js/app.js 的**连续区间** L870-L1058，内容一字未改（仅加这 3 行头）。
// classic script、共享全局作用域：加载顺序见 templates/index.html，**顺序不可随意调整**。
// ================= 日志与状态 =================
let _logFirstLoad = true;
let _logViewDate = '';  // 空 = 今天（10s 轮询）；非空 = 历史日期（静态，不轮询）
let _lastRenderSnap = '';  // 轮询条件渲染：accounts+states 快照，无变化不重建 DOM
let _logLastSnap = null;  // 日志渲染快照：内容未变化时跳过重写，避免 10s 轮询全量 DOM 重建
let _logSearch = '';
let _logShowAll = false;
let _logCurDate = '';
function initLogSearch() {
  const inp = $('log-search');
  if (!inp || inp._bound) return;
  inp._bound = true;
  inp.addEventListener('input', () => {
    clearTimeout(inp._t);
    inp._t = setTimeout(() => { _logSearch = inp.value.trim(); _logFirstLoad = true; loadLogs(); }, 300);
  });
}
function toggleLogAll() {
  _logShowAll = !_logShowAll;
  const btn = $('log-all-btn');
  if (btn) btn.textContent = _logShowAll ? '回到最近' : '显示全部';
  _logFirstLoad = true;
  loadLogs();
}
function exportLog() {
  const d = _logViewDate || _logCurDate;
  if (!d) { toast('暂无可导出的日志日期', true); return; }
  window.open('/api/logs/export?date=' + encodeURIComponent(d), '_blank');
}
async function loadLogs() {
  try {
    const params = new URLSearchParams();
    if (_logViewDate) params.set('date', _logViewDate);
    if (_logSearch) params.set('q', _logSearch);
    if (_logShowAll) params.set('all', '1');
    const qs = params.toString();
    const data = await api('/api/logs' + (qs ? '?' + qs : ''));
    _logCurDate = data.date;
    $('log-lines-info').textContent = _logSearch
      ? `${data.returned} 行匹配 / 共 ${data.total_lines} 行`
      : (data.truncated
        ? `已截断：显示前 ${data.returned} / 共 ${data.total_lines} 行（导出可取完整文件）`
        : (data.total_lines > 80 ? `共 ${data.total_lines} 行（默认显示最后 80 行，可显示全部或导出）` : `共 ${data.total_lines} 行`));
    // 注意：不再写 state.states——账号表格图标/统计卡的事实源是 /api/accounts 轮询
    // （sign-state 文件状态码）；/api/logs 只提供日志行（2026-08-16 审查轮修复）
    $('log-file').textContent = data.log_file;
    // 日期视图提示：历史日期静态展示 + 「回到今天」按钮
    // 当今天无日志时，后端返回最近有日志的一天（is_today=false）
    if (_logViewDate || !data.is_today) {
      $('log-date-tip').textContent = `正在查看 ${data.date} 的日志（历史日期不自动刷新）`;
      $('log-back-today').classList.remove('hidden');
    } else {
      $('log-date-tip').textContent = '';
      $('log-back-today').classList.add('hidden');
    }
    const box = $('log-box');
    const empty = $('log-empty');
    // 展示层脱敏：日志行内 [13800138000] 前缀 → [138****8000]（sign.log 文件保持完整供状态解析）
    const rendered = data.logs.map(l => l.replace(/\[(\d{11})\]/g, (m, p) => '[' + maskPhone(p) + ']')).join('\n');
    const renderedProbe = (data.probe_events || []).map(e =>
      `${e.time} [${e.phone}] ${e.status === 'failed' ? '❌异常' : '✅正常'}${e.message ? ' · ' + e.message : ''}`
    ).join('\n');
    const probeBlock = $('probe-block');
    if ((data.probe_events || []).length) {
      $('probe-count').textContent = data.probe_events.length;
      $('probe-box').textContent = renderedProbe;
      probeBlock.classList.remove('hidden');
    } else {
      probeBlock.classList.add('hidden');
    }
    // 签到事件时间线（sign_events 补消费端），符号按状态码（与日志口径一致）
    const _sievSym = { success: '✅', already: '✅', no_task: '➖', failed: '❌',
      retrying: '🔄', pending: '🕐', skipped_window: '⛔', skipped_norange: '⛔',
      paused: '⏸️', user_cancelled: '⏹️' };
    const renderedSignEv = (data.sign_events || []).map(e =>
      `${e.time} [${e.phone}] ${_sievSym[e.status] || '·'} ${e.status}${e.attempt > 1 ? '（第' + e.attempt + '次）' : ''}${e.message ? ' · ' + e.message : ''}`
    ).join('\n');
    const sievBlock = $('signev-block');
    if ((data.sign_events || []).length) {
      $('signev-count').textContent = data.sign_events.length;
      $('signev-box').textContent = renderedSignEv;
      sievBlock.classList.remove('hidden');
    } else {
      sievBlock.classList.add('hidden');
    }
    // 快照纳入探针区，内容变化时强制重渲染（否则轮询会因 logs 未变而跳过）
    const snap = (data.logs.length ? rendered : '') + '\n@@PROBE@@' + renderedProbe + '\n@@SIGNEV@@' + renderedSignEv + '\n@@INFO@@' + $('log-lines-info').textContent;
    if (snap === _logLastSnap) return;  // 日志无变化：不重写 DOM
    _logLastSnap = snap;
    if (data.logs.length) {
      const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
      box.textContent = rendered;
      box.classList.remove('hidden');
      empty.classList.add('hidden');
      if (nearBottom || _logFirstLoad) { box.scrollTop = box.scrollHeight; _logFirstLoad = false; }
    } else {
      box.classList.add('hidden');
      empty.textContent = _logSearch ? '（无匹配日志行）' : (_logViewDate ? `（${_logViewDate} 无签到日志）` : '（暂无签到日志，等待定时任务执行…）');
      empty.classList.remove('hidden');
    }
  } catch (e) { /* 静默：轮询失败不打扰 */ }
}

function viewLogDate() {
  const v = $('log-date').value;
  if (!v) { toast('请先选择日期', true); return; }
  _logViewDate = v;
  _logLastSnap = null;  // 强制重渲染（历史日期内容可能与当前快照相同）
  _logFirstLoad = true;
  loadLogs();
}

function backToTodayLogs() {
  _logViewDate = '';
  $('log-date').value = '';
  _logLastSnap = null;
  _logFirstLoad = true;
  loadLogs();
}

async function refreshAccountsSilent() {
  // 轮询拉取账号列表（静默失败）：管理员停留页面时同步审核状态/分组/统计卡
  // 条件渲染：数据（accounts + states）无变化时不重建 DOM，避免 10s 全量 reflow
  try {
    const data = await api('/api/accounts');
    state.states = data.states;  // /api/accounts 自带状态（键已脱敏），账号表格图标不再依赖日志轮询
    state.state_msgs = data.state_msgs || {};  // 状态原因/计划时间（表格 title 展示）
    state.state_durs = data.state_durs || {};  // 单次签到耗时（P6，表格 title 展示）
    const snap = JSON.stringify(data.accounts) + '|' + JSON.stringify(data.states);
    if (snap !== _lastRenderSnap) {
      _lastRenderSnap = snap;
      state.accounts = data.accounts;
      renderAccounts();
    }
  } catch (e) { /* 轮询失败不打扰 */ }
}

// 可见性轮询：仅当前可见的 tab 才请求对应接口（logs/accounts 都不可见时零请求），
// 避免后台页停留时持续拉日志/解密账号列表的服务端放大
function pollVisible() {
  const logsVisible = !$('tab-logs').classList.contains('hidden');
  const accountsVisible = !$('tab-accounts').classList.contains('hidden');
  // 历史日期日志静态展示：仅当天视图参与 10s 轮询（历史内容不会变化）
  if (logsVisible && !_logViewDate) loadLogs();
  if (accountsVisible) refreshAccountsSilent();
}

function fillSigninSelect() {
  const sel = $('signin-select');
  const current = sel.value;
  sel.innerHTML = '';
  // 只列可签到账号：已生效（active）且未被软删除；待审核/已拒绝/已删除不可手动签到
  const signable = state.accounts.filter(a => a.status === 'active' && !a.deleted);
  if (!signable.length) {
    sel.innerHTML = '<option value="">暂无签到账号</option>';
    return;
  }
  signable.forEach(a => {
    const opt = document.createElement('option');
    opt.value = a.index;  // value 用索引（列表已脱敏，完整号按需从详情接口取）
    opt.textContent = `${a.display_name} (${maskPhone(a.phone)})`;
    sel.appendChild(opt);
  });
  if (current) sel.value = current;
}

// 手动签到（下拉）：value 为账号索引 → 按需取完整手机号
async function doSigninFromSelect() {
  const v = $('signin-select').value;
  if (!v) { toast('请先在账号管理中配置账号', true); return; }
  doSigninByIndex(Number(v));
}

// 手动签到（行菜单/下拉）：通过详情接口取完整手机号后触发
async function doSigninByIndex(idx) {
  try {
    const d = await api(`/api/accounts/${idx}/detail`);
    doSignin(d.account.phone);
  } catch (e) { toast(e.message, true); }
}

async function doSignin(phone) {
  if (!phone) { toast('请先在账号管理中配置账号', true); return; }
  try {
    const data = await api('/api/signin', { method: 'POST', body: JSON.stringify({ phone }) });
    toast(data.msg || '已触发签到');
  } catch (e) { toast(e.message, true); }
}

