// 易班自动签到 · 管理端脚本 —— 管理员「我的账号」：调度自选时间片 + 签到日历
// 本文件是 web/static/js/app.js 的**连续区间** L2360-L2765，内容一字未改（仅加这 3 行头）。
// classic script、共享全局作用域：加载顺序见 templates/index.html，**顺序不可随意调整**。
// ===== 调度 v2：管理端自选时间片（与用户端同接口，管理员绑定 owner=admin 账号） =====
let minePrefCollapsed = false;

function toggleMinePrefCollapse() {
  minePrefCollapsed = !minePrefCollapsed;
  $('mine-pref-body').classList.toggle('hidden', minePrefCollapsed);
  $('mine-pref-collapse-btn').innerHTML = minePrefCollapsed ? icon('chevR') + ' 未开启（点击展开预配置）' : icon('chevD');
}

async function loadMineTimePref(preserve = false) {
  // preserve=true：修改后的局部刷新——不重置折叠状态，避免页面跳动
  const card = $('mine-pref-card');
  try {
    const data = await api('/api/my-time-pref');
    if (!data.has_account) { card.classList.add('hidden'); return; }
    card.classList.remove('hidden');
    $('mine-pref-window').textContent = data.window;
    renderMinePrefSlots(data);
    $('mine-pref-disabled-hint').classList.toggle('hidden', data.allowed);
    // 预计签到时段：开启且有自选 → 以自选片为准；未开启时预选不激活 → 显示真实调度时段
    const est = $('mine-pref-estimate');
    if (data.allowed && data.pref) {
      est.textContent = '';
      est.classList.add('hidden');
    } else if (data.estimated) {
      est.innerHTML = icon('cal') + ' 预计签到时段：' + esc(data.estimated || '') + esc(data.estimate_note || '')
        + (data.allowed ? '' : '（自选未开启，按自动分配）');
      est.classList.remove('hidden');
    } else {
      est.textContent = data.estimate_note || '';
      est.classList.remove('hidden');
    }
    if (!preserve) {
      if (!data.allowed) {
        minePrefCollapsed = true;
        $('mine-pref-body').classList.add('hidden');
        $('mine-pref-collapse-btn').innerHTML = icon('chevR') + ' 未开启（点击展开预配置）';
      } else {
        minePrefCollapsed = false;
        $('mine-pref-body').classList.remove('hidden');
        $('mine-pref-collapse-btn').innerHTML = icon('chevD');
      }
    }
  } catch (e) { card.classList.add('hidden'); }
}

function renderMinePrefSlots(data) {
  const grid = $('mine-pref-slot-grid');
  grid.innerHTML = '';
  let tip = '';  // 未开启时底部提示默认为空——黄字提示（mine-pref-disabled-hint）承担完整说明（2026-08-15 文案审查去重）
  data.slots.forEach((s, i) => {
    const sel = data.pref_slot === s.slot_min;
    const full = s.pct >= 100;  // 拥挤度百分比（2026-08-15：API 只下发 pct，与用户端一致）
    // 完全落入掐头去尾裁剪区（0.22.0）：灰色禁用，不可选择
    if (s.disabled) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.disabled = true;
      btn.className = 'rounded-lg border px-2 py-1.5 min-h-[44px] text-xs text-center cursor-not-allowed bg-zinc-100 dark:bg-zinc-900 border-zinc-200 dark:border-zinc-800 text-zinc-400 dark:text-zinc-600';
      btn.title = '该时段被掐头去尾保留，不可选择';
      btn.innerHTML = `<div class="font-medium">${esc(s.label)}</div><div class="opacity-70 whitespace-nowrap text-[11px] sm:text-xs">已保留</div>`;
      grid.appendChild(btn);
      return;
    }
    const partial = !!s.edge_note;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'rounded-lg border px-2 py-1.5 min-h-[44px] text-xs text-center transition-colors duration-150 ' +
      (sel
        ? 'bg-blue-600 border-blue-600 text-white'
        : full
          ? 'bg-amber-50 dark:bg-amber-900/20 border-amber-300 dark:border-amber-700 text-amber-700 dark:text-amber-400'
          : partial
            ? 'bg-amber-50/50 dark:bg-amber-900/10 border-dashed border-amber-400 dark:border-amber-600 text-amber-700 dark:text-amber-400'
            : 'bg-white dark:bg-zinc-800 border-zinc-200 dark:border-zinc-700 text-zinc-600 dark:text-zinc-300 hover:border-blue-400');
    if (partial) btn.title = s.edge_note + '，选中后将在可用部分签到';
    // 拥挤度：满员=100%+黄底警示已传达"已选满"，不再拼冗余前缀（移动端 4 列窄屏换行，2026-08-15）
    btn.innerHTML = `<div class="font-medium">${esc(s.label)}</div><div class="opacity-70 whitespace-nowrap text-[11px] sm:text-xs">已选${s.pct}%</div>`;
    btn.onclick = () => pickMineTimePref(s.slot_min);
    grid.appendChild(btn);
    if (sel && (i === 0 || i === data.slots.length - 1)) {
      const edgeTip = s.edge_note
        ? s.edge_note + '，选中后将在可用部分签到'
        : i === 0
          ? '最早时段：窗口开始后最先为你签到'
          : '最后时段：临近窗口截止执行，网络波动可能导致错过';
      tip = (data.allowed ? '' : '未开启：') + edgeTip;
    }
  });
  const tipEl = $('mine-pref-tip');
  tipEl.innerHTML = tip ? icon('warn') + ' ' + esc(tip) : '';
  // 警示语义上色：有提示时琥珀色（原版警示色），无提示回归中性
  tipEl.classList.toggle('text-amber-600', !!tip);
  tipEl.classList.toggle('dark:text-amber-400', !!tip);
}

async function pickMineTimePref(slot) {
  try {
    const data = await api('/api/my-time-pref', { method: 'PUT', body: JSON.stringify({ slot_min: slot }) });
    toast(data.msg || '已保存');
    loadMineTimePref(true);  // 局部刷新：保留当前展开态
  } catch (e) { toast(e.message, true); }
}

async function clearMineTimePref() {
  try {
    const data = await api('/api/my-time-pref', { method: 'PUT', body: JSON.stringify({ slot_min: null }) });
    toast(data.msg || '已清除');
    loadMineTimePref(true);  // 局部刷新：保留当前展开态
  } catch (e) { toast(e.message, true); }
}

function mineStatusBadge(status) {
  if (status === 'pending') return '<span class="yb-badge yb-badge-warning">待审核</span>';
  if (status === 'active') return '<span class="yb-badge yb-badge-success">已生效</span>';
  return esc(status);
}

function renderMine() {
  const list = $('mine-list');
  list.innerHTML = '';
  $('mine-empty').classList.toggle('hidden', mineAccounts.length > 0);
  // 存在未删除账号时隐藏提交表单；全部被管理员删除时仍显示表单供重新提交（软删除不死路）
  $('mine-form').classList.toggle('hidden', mineAccounts.some(a => !a.deleted));
  $('mine-done').classList.toggle('hidden', mineAccounts.length === 0);
  mineAccounts.forEach((a, i) => {
    const card = document.createElement('div');
    card.className = 'border border-zinc-200 dark:border-zinc-700 rounded-xl p-4';
    const calKey = 'mine-' + i;  // DOM id 用索引键（避免手机号进 id，可枚举泄露）
    card.innerHTML = `
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-center gap-3">
          <span class="text-lg leading-none inline-flex text-zinc-500 dark:text-zinc-400" aria-hidden="true">${a.deleted ? icon('trash') : stateIconSvg(a.state_status)}</span>
          <div>
            <div class="font-medium text-zinc-900 dark:text-zinc-100 text-sm">${esc(a.display_name)}</div>
            <div class="font-mono text-xs text-zinc-500 dark:text-zinc-400">${esc(a.phone)}${a.phone_model ? ' · ' + esc(a.phone_model) : ''}</div>
            ${a.deleted ? '<div class="text-xs text-zinc-500 dark:text-zinc-400 mt-0.5">已被管理员删除，待管理员在账号列表的待删除区处理</div>'
              : (a.status === 'active' ? ((a.state_status === 'success' || a.state_status === 'already')
                ? '<div class="text-xs text-green-600 dark:text-green-400 mt-0.5">今日已完成签到</div>'
                : '<div class="text-xs text-zinc-500 dark:text-zinc-400 mt-0.5">前方排队 <span class="font-medium text-zinc-600 dark:text-zinc-300">' + esc(a.queue_ahead) + '</span> 人</div>') : '')}
          </div>
        </div>
        <div class="flex items-center gap-2">
          ${a.deleted ? '<span class="yb-badge yb-badge-neutral">已删除</span>'
            : mineStatusBadge(a.status)}
          ${a.deleted
            ? '<span class="text-sm text-zinc-500 dark:text-zinc-400">待管理员在账号列表的待删除区处理</span>'
            : `<button onclick="openMineEditInline(${i})" class="text-sm text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors duration-150">编辑</button>
          <button onclick="deleteMineAccount(${i})" class="text-sm text-red-600 dark:text-red-400 hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors duration-150">删除</button>`}
        </div>
      </div>
      ${a.logs && a.logs.length ? `
        <details class="mt-3">
          <summary class="text-xs text-zinc-500 dark:text-zinc-400 cursor-pointer hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors duration-150">最近签到记录（${a.logs.length} 条）</summary>
          <pre class="log-text text-zinc-600 dark:text-zinc-300 mt-2 p-3 yb-inset whitespace-pre-wrap break-all">${esc(a.logs.join('\n'))}</pre>
        </details>` : ''}
      ${!a.deleted && a.status === 'pending' ? '<div class="mt-4 text-xs text-zinc-500 dark:text-zinc-400">审核通过后即可查看签到日历</div>' : ''}<div class="mt-4 max-w-sm lg:max-w-none" id="cal-wrap-${calKey}"></div>
    `;
    list.appendChild(card);
    if (!a.deleted && a.status === 'active') renderCalendar(a.phone, calKey);  // 已删除/未生效账号不显示日历
  });
}

// 签到日历统一由 static/js/calendar.js 提供（window.renderCalendar）。
// 旧实现在 admin 与 user 两端各存一份、长期漂移；现收敛为唯一实现，
// 由 tests/test_web_calendar_parity.py 守卫"只此一份"。本文件只负责调用。

// 我的账号内联编辑（与普通用户一致：复用提交表单，编辑时预填）
// 清除已配置识别码（我的账号编辑）：标记后提交 __clear__（后端并行流已支持该标记清空字段）
let mineClearCodeFlag = false;
function toggleMineClearCode() {
  mineClearCodeFlag = !mineClearCodeFlag;
  const input = $('m-code');
  const btn = $('clear-mcode-btn');
  if (mineClearCodeFlag) {
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

let mineEditingIndex = null;
function openMineEditInline(i) {
  mineEditingIndex = i;
  const a = mineAccounts[i];
  $('m-name').value = a.name;
  $('m-phone').value = a.phone;
  $('m-password').value = '';
  $('m-password').placeholder = '留空表示不修改密码';
  $('m-password').removeAttribute('required');  // 编辑模式密码可留空（留空=不修改），添加模式保留必填
  $('m-model').value = a.phone_model;
  $('m-code').value = '';
  $('m-code').readOnly = false;
  $('m-code').placeholder = a.has_phone_code ? '留空表示不修改（已配置）' : '64 位十六进制识别码';
  mineClearCodeFlag = false;
  $('clear-mcode-btn').classList.toggle('hidden', !a.has_phone_code);
  $('mine-form-title').textContent = '编辑我的易班账号';
  $('mine-submit-btn').textContent = '保存修改';
  $('mine-cancel-btn').classList.remove('hidden');
  $('mine-done').classList.add('hidden');
  $('mine-form').classList.remove('hidden');
  $('mine-form').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });  // 减弱动效偏好下瞬时定位（修复④）
}

// 取消编辑：恢复提交态并收起表单（loadMine 按账号数重新隐藏）
function cancelMineEdit() {
  resetMineForm();
  loadMine();
  toast('已取消编辑');
}

function resetMineForm() {
  mineEditingIndex = null;
  mineClearCodeFlag = false;
  $('mine-form-title').textContent = '提交我的易班账号';
  $('mine-submit-btn').textContent = '提交账号';
  $('mine-cancel-btn').classList.add('hidden');
  $('m-password').placeholder = '用于自动登录签到';
  $('m-password').setAttribute('required', '');
  $('m-code').readOnly = false;
  $('m-code').placeholder = '64 位十六进制识别码';
  $('clear-mcode-btn').classList.add('hidden');
}

// 修改主管理员密码折叠块（v0.30.0 起并入设置页危险区）改用原生 <details>，
// 原 toggleMyPassword（button+hidden div 手工展开）随旧结构删除

let mineSubmitting = false;  // 在途锁（参照 scheduleSaving 模式）：提交期间按钮禁用，防连点重复提交
$('mine-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (mineSubmitting) return;
  mineSubmitting = true;
  const btn = $('mine-submit-btn');
  const btnText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '提交中…';
  const tip = $('mine-tip');
  tip.textContent = '提交中…';
  try {
    const body = {
      name: $('m-name').value,
      phone: $('m-phone').value.trim(),
      password: $('m-password').value,
      phone_model: $('m-model').value.trim(),
      // 已标记清除识别码 → 提交 __clear__ 由后端清空字段；否则留空表示不修改
      phone_code: mineClearCodeFlag ? '__clear__' : $('m-code').value.trim(),
    };
    const data = mineEditingIndex !== null
      ? await api(`/api/my-accounts/${mineEditingIndex}`, { method: 'PUT', body: JSON.stringify(body) })
      : await api('/api/my-accounts', { method: 'POST', body: JSON.stringify(body) });
    tip.textContent = '';
    resetMineForm();  // 成功路径由 resetMineForm 恢复按钮文案（「提交账号」）
    e.target.reset();
    toast(data.msg || '已保存');
    loadMine();
  } catch (err) {
    tip.textContent = '';
    btn.textContent = btnText;  // 失败恢复原文案
    toast(err.message, true);
  }
  finally {
    mineSubmitting = false;
    btn.disabled = false;
  }
});

async function deleteMineAccount(i) {
  const a = mineAccounts[i];
  if (!confirm(`确定删除「${jsEscape(a.display_name)}」(${a.phone}) 吗？`)) return;
  try {
    await api(`/api/my-accounts/${i}`, { method: 'DELETE' });
    toast('已删除');
    loadMine();
    loadAccounts();
  } catch (e) { toast(e.message, true); }
}

// 全局公告：访问时加载一次（登录页也显示，无需登录）
async function loadAnnouncement() {
  try {
    const resp = await fetch(BASE + '/api/announcement');
    const data = await resp.json().catch(() => ({}));
    const text = (data && data.text || '').trim();
    const bar = document.getElementById('announcement-bar');
    const el = document.getElementById('announcement-text');
    if (text && bar && el) {
      el.textContent = text;  // textContent 防 XSS
      bar.classList.remove('hidden');
    }
  } catch (e) { /* 静默 */ }
}
document.addEventListener('DOMContentLoaded', loadAnnouncement);
