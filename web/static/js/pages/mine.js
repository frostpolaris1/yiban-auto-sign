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

// ================= 签到日历（按日状态文件 + 日志，与用户端一致） =================
const calState = {};  // phone -> {year, month}（内存键用手机号防排序变化错位；DOM id 仍用索引键避免手机号进 id）

function calPad(n) { return String(n).padStart(2, '0'); }

function calShift(btn, delta) {
  const key = btn.dataset.key;     // DOM 查询键（索引）
  const phone = btn.dataset.phone; // 内存状态键（手机号）
  const st = calState[phone] || (calState[phone] = (() => { const d = new Date(); return { year: d.getFullYear(), month: d.getMonth() + 1 }; })());
  st.month += delta;
  if (st.month < 1) { st.month = 12; st.year--; }
  if (st.month > 12) { st.month = 1; st.year++; }
  renderCalendar(phone, key);
}

// ==== 签到日历 · 日期格（admin=app.js 与 user=user.html 各有一份，**两份必须逐字相同**；
//      由 tests/test_web_calendar_parity.py 钉住 —— 改一边必须同步改另一边，否则门禁会红）====
// 视觉借自 daisyUI 日历的状态手法（**用底色表达状态，而不是只改文字颜色**），
// 但通道分配按本项目的**信息层级**重排：
//   ① 底色  = 签到结果（用户最想扫读的信息，给最强的视觉通道）：
//              ✅ green-50/700  ❌ red-50/700  周末停签 zinc-100/600（中性）
//   ② ring  = 今天（不占布局，与底色互不争夺，可叠加）；
//   ③ 角标  = 「休」（周末停签），不单靠颜色区分。
// 注：daisyUI 把「今天」做成实心主色底；**我们没照抄** —— 那会与「已签到」的绿底
// 抢同一个通道。底色留给签到结果，今天改用 ring。
// ⚠ 周末停签**不**沿用旧的「把数字做很浅」（那是 zinc-300 = 1.42:1，连日期都读不出）：
//   该格仍可点（点了提示「周日无需签到」），属**有信息**的格子，不是 WCAG 1.4.3 豁免的
//   非活动控件，故一并改用底色表达，数字保持 7.03:1（浅）/ 10.08:1（暗）。
function calDayCell(o) {
  let cls = 'relative aspect-square rounded-lg flex items-center justify-center text-xs transition-colors duration-150 cursor-pointer ';
  if (o.off) cls += 'bg-zinc-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-200 dark:hover:bg-zinc-700';
  else if (o.state === '✅') cls += 'bg-green-50 dark:bg-green-900/25 text-green-700 dark:text-green-400 font-medium hover:bg-green-100 dark:hover:bg-green-900/40';
  else if (o.state === '❌') cls += 'bg-red-50 dark:bg-red-900/25 text-red-700 dark:text-red-400 font-medium hover:bg-red-100 dark:hover:bg-red-900/40';
  else cls += 'text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-700';
  if (o.isToday) cls += ' ring-2 ring-inset ring-blue-500 dark:ring-blue-400';
  const off = o.offDay ? '（周' + o.offDay + '不签到）' : '';
  const label = o.date + (o.isToday ? '，今天' : '')
    + (o.state === '✅' ? '，已签到' : o.state === '❌' ? '，签到失败' : (off ? '' : '，查看签到记录'))
    + off;
  const badge = o.offDay ? '<span class="absolute top-0.5 right-1 text-[10px] leading-none text-zinc-600 dark:text-zinc-400">休</span>' : '';
  return `<button type="button" onclick="calLoadLog(this, '${o.date}')" data-key="${esc(o.key)}" data-phone="${esc(o.phone)}" title="${o.date}" aria-label="${label}" class="${cls}"><span>${o.d}</span>${badge}</button>`;
}

function renderCalendar(phone, key) {
  const wrap = $('cal-wrap-' + key);
  if (!wrap) return;
  const st = calState[phone] || (calState[phone] = (() => { const d = new Date(); return { year: d.getFullYear(), month: d.getMonth() + 1 }; })());
  const { year, month } = st;
  const monthStr = `${year}-${calPad(month)}`;
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${calPad(today.getMonth() + 1)}-${calPad(today.getDate())}`;
  wrap.innerHTML = `
    <div class="grid lg:grid-cols-2 gap-4">
      <div>
        <div class="flex items-center justify-between mb-2">
          <div class="text-sm font-medium text-zinc-700 dark:text-zinc-200">签到日历 · ${year}年${month}月</div>
          <div class="flex items-center gap-1">
            <button onclick="calShift(this, -1)" data-key="${esc(key)}" data-phone="${esc(phone)}" aria-label="上个月" class="w-8 h-8 flex items-center justify-center rounded-lg text-sm text-zinc-500 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-700 transition-colors duration-150">${icon('chevL')}</button>
            <button onclick="calShift(this, 1)" data-key="${esc(key)}" data-phone="${esc(phone)}" aria-label="下个月" class="w-8 h-8 flex items-center justify-center rounded-lg text-sm text-zinc-500 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-700 transition-colors duration-150">${icon('chevR')}</button>
          </div>
        </div>
        <div id="cal-grid-${key}" class="grid grid-cols-7 gap-1"></div>
      </div>
      <div id="cal-log-${key}" class="lg:pt-8"></div>
    </div>`;
  const grid = $('cal-grid-' + key);
  grid.innerHTML = ['一','二','三','四','五','六','日'].map(w =>
    `<div class="text-center text-xs text-zinc-500 dark:text-zinc-400 py-1">${w}</div>`).join('');
  api(`/api/my-calendar?month=${monthStr}`).then(data => {
    state.sundaySign = !!data.sunday_sign;  // 与 user.html 一致：开启周日签到后周日正常显示/可查
    state.saturdaySign = data.saturday_sign === 1;  // 周六签到（默认关闭，v0.29.0 起），开启后照常/可查
    const firstDay = (new Date(year, month - 1, 1).getDay() + 6) % 7;  // 周一起始
    const days = new Date(year, month, 0).getDate();
    for (let i = 0; i < firstDay; i++) {
      grid.insertAdjacentHTML('beforeend', '<div></div>');
    }
    for (let d = 1; d <= days; d++) {
      const date = `${monthStr}-${calPad(d)}`;
      const stt = data.days && data.days[date] ? data.days[date][phone] || '' : '';
      const wd = new Date(year, month - 1, d).getDay();
      const sunOff = wd === 0 && !state.sundaySign;    // 周日停签且开关关闭
      const satOff = wd === 6 && !state.saturdaySign;  // 周六停签且开关关闭（v0.29.0 起默认关）
      const off = sunOff || satOff;
      grid.insertAdjacentHTML('beforeend', calDayCell({
        d, date, key, phone, state: stt, off,
        offDay: off ? (sunOff ? '日' : '六') : '', isToday: date === todayStr,
      }));
    }
  }).catch(() => {
    grid.innerHTML = '<div class="col-span-7 text-center text-xs text-zinc-500 dark:text-zinc-400 py-4">日历加载失败，请稍后重试</div>';
  });
}

function calLoadLog(btn, date) {
  const key = btn.dataset.key;
  const box = $('cal-log-' + key);
  // 周六/周日无需签到（各自开关关闭时），直接提示（不查询日志）
  const wd0 = new Date(date + 'T00:00:00').getDay();
  if ((wd0 === 0 && !state.sundaySign) || (wd0 === 6 && !state.saturdaySign)) {
    box.innerHTML = `<div class="text-xs text-zinc-500 dark:text-zinc-400 p-3 bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-lg">${wd0 === 0 ? '周日' : '周六'}无需签到</div>`;
    return;
  }
  box.innerHTML = '<div class="text-xs text-zinc-500 dark:text-zinc-400"><span class="yb-spinner"></span> 加载中…</div>';
  api(`/api/my-logs?date=${date}`).then(data => {
    if (!data.logs || !data.logs.length) {
      box.innerHTML = `<div class="text-xs text-zinc-500 dark:text-zinc-400 p-3 bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-lg">${date} 暂无签到记录</div>`;
      return;
    }
    box.innerHTML = `<div class="text-xs text-zinc-500 dark:text-zinc-400 mb-1">${date} 签到记录（${data.logs.length} 条）</div>
      <pre class="log-text text-zinc-600 dark:text-zinc-300 p-3 yb-inset whitespace-pre-wrap break-all">${esc(data.logs.join(String.fromCharCode(10)))}</pre>`;
  }).catch(e => { box.innerHTML = ''; toast(e.message, true); });
}

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
